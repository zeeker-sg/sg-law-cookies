"""News-only admission: explicit plans, consistent backups, atomic batches.

This module never builds, uploads, posts messages, runs ingestion, or changes
the shared 72-hour policy. Unresolved ontology tags remain warnings.
"""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from sg_law_cookies import db
from sg_law_cookies.models import Cookie


class NewsApprovalError(ValueError):
    pass


def open_news_db(path: Path, *, writable: bool = False) -> sqlite3.Connection:
    mode = "rw" if writable else "ro"  # Never create/initialise a live database.
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=" + mode,
                           uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not writable:
        conn.execute("PRAGMA query_only=ON")
    return conn


def _file_identity(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino


def validate_news_cookie(conn: sqlite3.Connection, cookie: Cookie) -> None:
    if any(not text.strip() for text in
           (cookie.headline, cookie.summary, cookie.why_it_matters)):
        raise NewsApprovalError("blank_required_text")
    if not cookie.source_ids or len(set(cookie.source_ids)) != len(cookie.source_ids):
        raise NewsApprovalError("missing_or_duplicate_source_ids")
    for sid in cookie.source_ids:
        source = db.get_source(conn, sid)
        if source is None or source.item_type != "news":
            raise NewsApprovalError("missing_or_nonnews_source")
    if cookie.created_at.tzinfo is None or cookie.created_at > datetime.now(timezone.utc):
        raise NewsApprovalError("invalid_creation_time")


@dataclass
class NewsPlan:
    cookies: list[Cookie]
    skipped: list[dict]
    excluded: dict
    fingerprints: dict

    def report(self) -> dict:
        return {"eligible_count": len(self.cookies),
                "eligible_ids": [c.id for c in self.cookies],
                "fingerprints": self.fingerprints,
                "duplicates": {c.id: c.duplicate_of for c in self.cookies
                               if c.is_duplicate},
                "skipped": self.skipped, "excluded": self.excluded}


def plan_pending_news(conn: sqlite3.Connection) -> NewsPlan:
    """Read-only eligibility; never include rejected/regenerating/rejected-once rows."""
    rows = list(conn.execute("SELECT * FROM pending_cookies ORDER BY created_at,id"))
    live = list(conn.execute("SELECT id,headline,created_at FROM cookies "
                             "WHERE is_duplicate=0 ORDER BY created_at,id"))
    cookies, skipped, accepted, excluded, fingerprints = [], [], {}, {}, {}
    for row in rows:
        if row["item_type"] != "news":
            key = row["item_type"] + ":" + row["review_status"]
            excluded[key] = excluded.get(key, 0) + 1
            continue
        reason = None
        if row["review_status"] != "pending":
            reason = "not_pending"
        elif row["reject_count"] != 0 or row["reject_reason"]:
            reason = "rejection_history"
        try:
            if reason:
                raise NewsApprovalError(reason)
            if conn.execute("SELECT 1 FROM cookies WHERE id=?", (row["id"],)).fetchone():
                raise NewsApprovalError("already_live_pending_row")
            data = {k: row[k] for k in ("id", "headline", "summary", "why_it_matters",
                                        "significance", "created_at")}
            for key in ("folio_areas", "folio_entities", "folio_concepts",
                        "unresolved", "source_ids"):
                data[key] = json.loads(row[key])
            cookie = Cookie.model_validate(data)
            validate_news_cookie(conn, cookie)
            # Pending storage lacks duplicate flags. Reconstruct the existing
            # seven-day exact-headline rule, conservatively retaining ambiguous
            # duplicates of a still-reviewed/invalid pending original for review.
            headline = cookie.headline.strip().lower()
            cutoff = cookie.created_at - timedelta(days=7)
            original = next((r["id"] for r in live
                             if r["headline"].strip().lower() == headline
                             and datetime.fromisoformat(r["created_at"]) >= cutoff), None)
            if original is None:
                for earlier in rows:
                    if earlier["id"] == cookie.id:
                        break
                    if (earlier["review_status"] == "pending"
                            and earlier["headline"].strip().lower() == headline
                            and datetime.fromisoformat(earlier["created_at"]) >= cutoff):
                        previous = accepted.get(earlier["id"])
                        if previous is None:
                            raise NewsApprovalError("duplicate_of_pending_review")
                        original = previous.duplicate_of or previous.id
                        break
            if original:
                cookie.is_duplicate, cookie.duplicate_of = True, original
            cookies.append(cookie)
            accepted[cookie.id] = cookie
            evidence = {"cookie": cookie.model_dump(mode="json"),
                        "sources": [db.get_source(conn, sid).model_dump(mode="json")
                                    for sid in cookie.source_ids]}
            fingerprints[cookie.id] = hashlib.sha256(
                json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        except (NewsApprovalError, ValidationError, ValueError, TypeError, KeyError) as exc:
            # Do not expose draft text or validation inputs in reports/logs.
            code = str(exc) if isinstance(exc, NewsApprovalError) else "invalid_cookie_data"
            skipped.append({"id": row["id"], "reason": code})
    return NewsPlan(cookies, skipped, excluded, fingerprints)


def apply_pending_news(path: Path, backup: Path, expected: dict) -> dict:
    """Apply only the reviewed plan under a write lock, with a pre-write backup.

    A second invocation of a completed plan is a no-op. New or edited pending
    rows cannot be silently substituted into the approved batch.
    """
    identity = _file_identity(path)
    conn = open_news_db(path, writable=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if _file_identity(path) != identity:
            raise NewsApprovalError("database replaced; retry with cookie cycle idle")
        plan = plan_pending_news(conn)
        report = plan.report()
        wanted = set(expected["eligible_ids"])
        if len(wanted) != len(expected["eligible_ids"]):
            raise NewsApprovalError("invalid_expected_plan")
        remaining, already = [], []
        by_id = {c.id: c for c in plan.cookies}
        for cid in wanted:
            if cid in by_id:
                if report["fingerprints"][cid] != expected["fingerprints"].get(cid):
                    raise NewsApprovalError("eligibility_or_content_drift; rerun dry-run")
                remaining.append(by_id[cid])
            elif (conn.execute("SELECT 1 FROM cookies WHERE id=?", (cid,)).fetchone()
                  and not conn.execute("SELECT 1 FROM pending_cookies WHERE id=?", (cid,)).fetchone()):
                already.append(cid)
            else:
                raise NewsApprovalError("eligibility_or_content_drift; rerun dry-run")
        if not remaining:
            conn.rollback()
            return {"approved_ids": [], "already_applied_ids": sorted(already), "backup": None}
        # A separate read connection can back up the committed WAL snapshot
        # while BEGIN IMMEDIATE prevents concurrent reviewers/ingestion writing.
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        reader = open_news_db(path)
        target = sqlite3.connect(backup)
        try:
            reader.backup(target)
            if target.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise NewsApprovalError("backup_integrity_check_failed")
        finally:
            reader.close()
            target.close()
        if _file_identity(path) != identity:
            raise NewsApprovalError("database replaced; retry with cookie cycle idle")
        # Keep canonical pending originals before their duplicate children.
        ordered = [c for c in plan.cookies if c.id in wanted]
        for cookie in ordered:
            db.save_cookie(conn, cookie, commit=False)
            if cookie.duplicate_of:
                conn.executemany("INSERT OR IGNORE INTO cookie_sources(cookie_id,source_id) "
                                 "VALUES (?,?)",
                                 [(cookie.duplicate_of, sid) for sid in cookie.source_ids])
            # Preserve deferred corroboration from earlier auto-admitted news
            # whose canonical source was still in the review queue.
            conn.execute("INSERT OR IGNORE INTO cookie_sources(cookie_id,source_id) "
                         "SELECT ?,cs.source_id FROM cookie_sources cs JOIN cookies c "
                         "ON c.id=cs.cookie_id WHERE c.duplicate_of=?",
                         (cookie.id, cookie.id))
            conn.execute("DELETE FROM pending_cookies WHERE id=? AND item_type='news' "
                         "AND review_status='pending' AND reject_count=0", (cookie.id,))
        if _file_identity(path) != identity:
            raise NewsApprovalError("database replaced; retry with cookie cycle idle")
        conn.commit()
        return {"approved_ids": [c.id for c in ordered],
                "already_applied_ids": sorted(already), "backup": str(backup)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
