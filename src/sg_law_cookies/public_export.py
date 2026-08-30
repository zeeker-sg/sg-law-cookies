"""Public export of the cookies dataset for data.zeeker.sg.

The Datasette JSON API on data.zeeker.sg (and the zeeker-mcp gateway built
on top of it) serves SG Law Cookies from ``s3://<bucket>/latest/sg-law-cookies.db``:
the Zeeker deploy convention is that a database placed in ``latest/`` gets
published. The cookies pipeline itself only writes backups under
``backups/sg-law-cookies/`` — never ``latest/``, because the production DB
holds non-public review state (pending_cookies, rejected_cookies) that must
not be served.

This module rebuilds that missing publishing step: it derives a clean,
public snapshot of the promoted cookies (and their judgment documents)
from the production DB, writes an upload-ready SQLite file, and applies
the datasette conventions the served database is expected to carry:

- one row per live cookie (``is_duplicate = 0``, ``admitted_at IS NOT NULL``);
- public fields only — no review state, no internal duplicate bookkeeping;
- ``_zeeker_updates`` metadata table recording row counts per resource;
- FTS5 index tables for full-text search (content-linked to their source
  table), matching the shape datasette's FTS wiring expects;
- stable, source-derived IDs where the upstream document defines identity
  (sha256 of the source URL / first-seen term, truncated to 12 hex chars,
  the same derivation the ingest layer uses for Zeeker row ids).

The daily message idiom is the one the site itself uses: a cookie "belongs"
to the day it was admitted to the live DB (``date(admitted_at)``), not the
day its source document was published — an admitted cookie may have been
written weeks earlier (source_date_surface).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

EXPORT_LATEST_KEY = "latest/sg-law-cookies.db"


class ExportError(RuntimeError):
    pass


@dataclass
class ExportResult:
    """What one export run produced (and shipped, when uploading)."""

    db_path: Path
    cookies: int
    judgments: int
    judgment_issues: int
    unresolved_terms: int
    s3_key: str | None = None
    s3_bucket: str | None = None
    size_bytes: int = 0


def _sha12(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


@dataclass(frozen=True)
class _CookieRow:
    id: str
    headline: str
    summary: str
    why_it_matters: str
    significance: str
    date: str
    item_type: str
    primary_area: str | None
    folio_areas: str
    folio_entities: str
    folio_concepts: str
    unresolved: str
    source_url: str
    source_title: str


def _dump_refs(refs: list) -> str:
    out = []
    for ref in refs:
        if hasattr(ref, "model_dump"):
            out.append(ref.model_dump(mode="json"))
        else:
            out.append(ref)
    return json.dumps(out, ensure_ascii=False)


def _primary_area(refs_json: str) -> str | None:
    """First FOLIO area label — mirrors skydata._primary_area()."""
    try:
        refs = json.loads(refs_json)
    except (TypeError, ValueError):
        return None
    for ref in refs or []:
        label = ref.get("preferred_label") if isinstance(ref, dict) else None
        if label:
            return label
    return None


def _cookie_rows(conn: sqlite3.Connection) -> list[_CookieRow]:
    """Live cookies joined to their single primary source.

    Every live cookie carries exactly one cookie_sources link in practice
    (initial v2 builds produced a single two-source cookie, and that
    mapping predates multiple rows per cookie); multi-source cookies are
    exported with the first link in source order so no cookie is dropped.
    """
    rows: list[_CookieRow] = []
    seen: set[str] = set()
    for (
        cookie_id,
        headline,
        summary,
        why,
        significance,
        admitted_at,
        source_url,
        source_title,
        item_type,
        source_date,
        folio_areas,
        folio_entities,
        folio_concepts,
        unresolved,
    ) in conn.execute(
        """
        SELECT c.id, c.headline, c.summary, c.why_it_matters, c.significance,
               c.admitted_at, s.source_url, s.title, s.item_type, s.date,
               c.folio_areas, c.folio_entities, c.folio_concepts, c.unresolved
        FROM cookies c
        JOIN cookie_sources cs ON cs.cookie_id = c.id
        JOIN sources s ON s.id = cs.source_id
        WHERE c.is_duplicate = 0 AND c.admitted_at IS NOT NULL
        ORDER BY c.id, cs.source_id
        """
    ):
        if cookie_id in seen:
            continue
        seen.add(cookie_id)
        rows.append(
            _CookieRow(
                id=cookie_id,
                headline=headline,
                summary=summary,
                why_it_matters=why,
                significance=significance,
                date=str(admitted_at)[:10],
                item_type=item_type,
                primary_area=_primary_area(folio_areas),
                folio_areas=folio_areas or "[]",
                folio_entities=folio_entities or "[]",
                folio_concepts=folio_concepts or "[]",
                unresolved=unresolved or "[]",
                source_url=source_url,
                source_title=source_title,
            )
        )
    return rows


def _judgment_rows(meta_rows) -> list[dict]:
    """One row per judgment document, keyed by its source URL hash.

    The v1 exporter keyed judgments the same way; reproducing the
    derivation keeps ids stable across the June 2026 gap for any document
    already published then.
    """
    out = []
    for meta in meta_rows:
        source_url = meta["source_url"]
        source_date = meta["source_date"]
        jid = _sha12(source_url)
        out.append(
            {
                "id": jid,
                "citation": meta["citation"],
                # v2 stores the case name on the source row, not in judgment_meta
                "case_name": meta["title"],
                "court": meta["court"],
                "judges": json.dumps(_loads_list(meta["judges"]), ensure_ascii=False),
                "parties": json.dumps(_loads_list(meta["parties"]), ensure_ascii=False),
                "legislation": json.dumps(_dump_refs(_loads_list(meta["legislation"])), ensure_ascii=False),
                "cases_cited": json.dumps(_dump_refs(_loads_list(meta["cases_cited"])), ensure_ascii=False),
                "orders": meta["orders"] or "",
                "issue_count": len(_loads_list(meta["issues"])),
                "date": source_date,
                "source_url": source_url,
            }
        )
    return out


def _issue_rows(meta_rows) -> list[dict]:
    out = []
    for meta in meta_rows:
        source_url = meta["source_url"]
        source_date = meta["source_date"]
        jid = _sha12(source_url)
        citation = meta["citation"]
        court = meta["court"] or ""
        for idx, issue in enumerate(_loads_list(meta["issues"])):
            out.append(
                {
                    "id": _sha12(f"{jid}::{idx}"),
                    "judgment_id": jid,
                    "citation": citation,
                    "court": court,
                    "issue_index": idx,
                    "question": issue.get("question", ""),
                    "holding": issue.get("holding", ""),
                    "reasoning": issue.get("reasoning", ""),
                    "concepts": _dump_refs(issue.get("folio_concepts", [])),
                    "date": source_date,
                    "source_url": source_url,
                }
            )
    return out


def _loads_list(raw) -> list:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def export_public_db(
    db_path: Path,
    out_path: Path,
    *,
    upload: bool = False,
    bucket: str | None = None,
) -> ExportResult:
    """Snapshot the production DB and derive the public export at out_path.

    Reads through a consistent SQLite backup copy (the pipeline may be
    writing concurrently). With ``upload=True`` the export is pushed to
    ``latest/sg-law-cookies.db`` in the S3 bucket, which the datasette
    refresh publishes to data.zeeker.sg (mcp.zeeker.sg inherits the same
    file via its datasette upstream).
    """
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = _cookie_rows(src)
        meta_rows = src.execute(
            """
            SELECT jm.*, s.source_url, s.title, s.date AS source_date
            FROM judgment_meta jm
            JOIN sources s ON s.id = jm.source_id
            ORDER BY jm.source_id
            """
        ).fetchall()
        meta_cols = [
            d[0]
            for d in src.execute(
                """
                SELECT jm.*, s.source_url, s.title, s.date AS source_date
                FROM judgment_meta jm
                JOIN sources s ON s.id = jm.source_id
                LIMIT 0
                """
            ).description
        ]
        meta_dicts = [dict(zip(meta_cols, r)) for r in meta_rows]
        judgments = _judgment_rows(meta_dicts)
        judgment_issues = _issue_rows(meta_dicts)
        unresolved_rows = [
            {
                "id": _sha12(term),
                "term": term,
                "first_seen_date": first_seen,
                "count": count,
            }
            for term, first_seen, count in src.execute(
                "SELECT term, first_seen_date, count FROM unresolved_terms ORDER BY term"
            )
        ]
    finally:
        src.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".db.tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        con.executescript(
            """
            CREATE TABLE cookies (
                id TEXT PRIMARY KEY,
                headline TEXT NOT NULL,
                summary TEXT NOT NULL,
                why_it_matters TEXT NOT NULL,
                significance TEXT NOT NULL,
                date TEXT,
                item_type TEXT,
                primary_area TEXT,
                folio_areas TEXT,
                folio_entities TEXT,
                folio_concepts TEXT,
                unresolved TEXT,
                is_duplicate INTEGER NOT NULL DEFAULT 0,
                source_url TEXT NOT NULL,
                source_title TEXT
            );
            CREATE TABLE judgments (
                id TEXT PRIMARY KEY,
                citation TEXT,
                case_name TEXT,
                court TEXT,
                judges TEXT,
                parties TEXT,
                legislation TEXT,
                cases_cited TEXT,
                orders TEXT,
                issue_count INTEGER,
                date TEXT,
                source_url TEXT NOT NULL
            );
            CREATE TABLE judgment_issues (
                id TEXT PRIMARY KEY,
                judgment_id TEXT,
                citation TEXT,
                court TEXT,
                issue_index INTEGER,
                question TEXT,
                holding TEXT,
                reasoning TEXT,
                concepts TEXT,
                date TEXT,
                source_url TEXT
            );
            CREATE TABLE unresolved_terms (
                id TEXT PRIMARY KEY,
                term TEXT,
                first_seen_date TEXT,
                count INTEGER
            );
            CREATE TABLE _zeeker_updates (
                resource_name TEXT PRIMARY KEY,
                last_updated TEXT,
                record_count INTEGER,
                build_id TEXT,
                duration_ms INTEGER
            );
            """
        )
        con.executemany(
            "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r.id, r.headline, r.summary, r.why_it_matters, r.significance,
                    r.date, r.item_type, r.primary_area, r.folio_areas,
                    r.folio_entities, r.folio_concepts, r.unresolved,
                    # always 0 in the public export: duplicate-flagged rows
                    # are never exported (the column exists for API parity
                    # with the served schema)
                    0,
                    r.source_url, r.source_title,
                )
                for r in rows
            ],
        )
        con.executemany(
            "INSERT INTO judgments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    j["id"], j["citation"], j["case_name"], j["court"], j["judges"],
                    j["parties"], j["legislation"], j["cases_cited"], j["orders"],
                    j["issue_count"], j["date"], j["source_url"],
                )
                for j in judgments
            ],
        )
        con.executemany(
            "INSERT INTO judgment_issues VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    i["id"], i["judgment_id"], i["citation"], i["court"], i["issue_index"],
                    i["question"], i["holding"], i["reasoning"], i["concepts"],
                    i["date"], i["source_url"],
                )
                for i in judgment_issues
            ],
        )
        con.executemany(
            "INSERT INTO unresolved_terms VALUES (?,?,?,?)",
            [(u["id"], u["term"], u["first_seen_date"], u["count"]) for u in unresolved_rows],
        )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        started = datetime.now(timezone.utc)
        con.executemany(
            "INSERT INTO _zeeker_updates VALUES (?,?,?,?,?)",
            [
                ("cookies", stamp, len(rows), f"cookies-export-{stamp}", 0),
                ("judgments", stamp, len(judgments), f"cookies-export-{stamp}", 0),
                ("judgment_issues", stamp, len(judgment_issues), f"cookies-export-{stamp}", 0),
                ("unresolved_terms", stamp, len(unresolved_rows), f"cookies-export-{stamp}", 0),
            ],
        )
        con.commit()
        # FTS last: content-linked FTS5 mirrors + reconciliation triggers.
        # The triggers only fire on future writes, so populate the index
        # for the rows just inserted with the FTS5 'rebuild' command.
        con.executescript(
            """
            CREATE VIRTUAL TABLE cookies_fts USING FTS5 (
                headline, summary, why_it_matters,
                content=[cookies]
            );
            CREATE TRIGGER cookies_ai AFTER INSERT ON cookies BEGIN
              INSERT INTO cookies_fts(rowid, headline, summary, why_it_matters)
              VALUES (new.rowid, new.headline, new.summary, new.why_it_matters);
            END;
            CREATE TRIGGER cookies_ad AFTER DELETE ON cookies BEGIN
              INSERT INTO cookies_fts(cookies_fts, rowid, headline, summary, why_it_matters)
              VALUES('delete', old.rowid, old.headline, old.summary, old.why_it_matters);
            END;
            CREATE TRIGGER cookies_au AFTER UPDATE ON cookies BEGIN
              INSERT INTO cookies_fts(cookies_fts, rowid, headline, summary, why_it_matters)
              VALUES('delete', old.rowid, old.headline, old.summary, old.why_it_matters);
              INSERT INTO cookies_fts(rowid, headline, summary, why_it_matters)
              VALUES (new.rowid, new.headline, new.summary, new.why_it_matters);
            END;
            CREATE VIRTUAL TABLE unresolved_terms_fts USING FTS5 (
                term,
                content=[unresolved_terms]
            );
            CREATE TRIGGER unresolved_terms_ai AFTER INSERT ON unresolved_terms BEGIN
              INSERT INTO unresolved_terms_fts(rowid, term) VALUES (new.rowid, new.term);
            END;
            CREATE TRIGGER unresolved_terms_ad AFTER DELETE ON unresolved_terms BEGIN
              INSERT INTO unresolved_terms_fts(unresolved_terms_fts, rowid, term)
              VALUES('delete', old.rowid, old.term);
            END;
            CREATE TRIGGER unresolved_terms_au AFTER UPDATE ON unresolved_terms BEGIN
              INSERT INTO unresolved_terms_fts(unresolved_terms_fts, rowid, term)
              VALUES('delete', old.rowid, old.term);
              INSERT INTO unresolved_terms_fts(rowid, term) VALUES (new.rowid, new.term);
            END;
            CREATE VIRTUAL TABLE judgments_fts USING FTS5 (
                case_name, orders,
                content=[judgments]
            );
            CREATE TRIGGER judgments_ai AFTER INSERT ON judgments BEGIN
              INSERT INTO judgments_fts(rowid, case_name, orders)
              VALUES (new.rowid, new.case_name, new.orders);
            END;
            CREATE TRIGGER judgments_ad AFTER DELETE ON judgments BEGIN
              INSERT INTO judgments_fts(judgments_fts, rowid, case_name, orders)
              VALUES('delete', old.rowid, old.case_name, old.orders);
            END;
            CREATE TRIGGER judgments_au AFTER UPDATE ON judgments BEGIN
              INSERT INTO judgments_fts(judgments_fts, rowid, case_name, orders)
              VALUES('delete', old.rowid, old.case_name, old.orders);
              INSERT INTO judgments_fts(rowid, case_name, orders)
              VALUES (new.rowid, new.case_name, new.orders);
            END;
            CREATE VIRTUAL TABLE judgment_issues_fts USING FTS5 (
                question, holding, reasoning,
                content=[judgment_issues]
            );
            CREATE TRIGGER judgment_issues_ai AFTER INSERT ON judgment_issues BEGIN
              INSERT INTO judgment_issues_fts(rowid, question, holding, reasoning)
              VALUES (new.rowid, new.question, new.holding, new.reasoning);
            END;
            CREATE TRIGGER judgment_issues_ad AFTER DELETE ON judgment_issues BEGIN
              INSERT INTO judgment_issues_fts(judgment_issues_fts, rowid, question, holding, reasoning)
              VALUES('delete', old.rowid, old.question, old.holding, old.reasoning);
            END;
            CREATE TRIGGER judgment_issues_au AFTER UPDATE ON judgment_issues BEGIN
              INSERT INTO judgment_issues_fts(judgment_issues_fts, rowid, question, holding, reasoning)
              VALUES('delete', old.rowid, old.question, old.holding, old.reasoning);
              INSERT INTO judgment_issues_fts(rowid, question, holding, reasoning)
              VALUES (new.rowid, new.question, new.holding, new.reasoning);
            END;

            INSERT INTO cookies_fts(cookies_fts) VALUES('rebuild');
            INSERT INTO unresolved_terms_fts(unresolved_terms_fts) VALUES('rebuild');
            INSERT INTO judgments_fts(judgments_fts) VALUES('rebuild');
            INSERT INTO judgment_issues_fts(judgment_issues_fts) VALUES('rebuild');
            """
        )
        con.commit()
        # integrity_check to catch FTS/trigger wiring mistakes before upload
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ExportError(f"export DB failed integrity_check: {integrity}")
    finally:
        con.close()

    shutil.move(str(tmp), str(out_path))

    result = ExportResult(
        db_path=out_path,
        cookies=len(rows),
        judgments=len(judgments),
        judgment_issues=len(judgment_issues),
        unresolved_terms=len(unresolved_rows),
        size_bytes=out_path.stat().st_size,
    )

    if upload:
        bucket = bucket or os.getenv("S3_BUCKET")
        if not bucket:
            raise ExportError("S3_BUCKET is required for --upload (set it in .env)")
        import boto3
        from botocore.config import Config

        access_key = os.getenv("AWS_ACCESS_KEY_ID")
        secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        if not access_key or not secret_key:
            raise ExportError(
                "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required for --upload"
            )
        client = boto3.client(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            config=Config(
                response_checksum_validation="when_required",
                request_checksum_calculation="when_required",
            ),
        )
        client.upload_file(str(out_path), bucket, EXPORT_LATEST_KEY)
        result.s3_bucket = bucket
        result.s3_key = EXPORT_LATEST_KEY

    return result