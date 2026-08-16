"""SQLite storage layer (PRD sections 3.2 Layer 3, 5)."""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sg_law_cookies.models import (
    CaseCitation,
    Cookie,
    DailyStats,
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    Source,
    SourceRegistryEntry,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          TEXT PRIMARY KEY,
    source_url  TEXT NOT NULL,
    zeeker_url  TEXT NOT NULL,
    title       TEXT NOT NULL,
    raw_text    TEXT NOT NULL,
    date        TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    item_type   TEXT NOT NULL CHECK (item_type IN ('news', 'judgment')),
    license     TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    ingested_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_source_url ON sources (source_url);

CREATE TABLE IF NOT EXISTS cookies (
    id             TEXT PRIMARY KEY,
    headline       TEXT NOT NULL,
    summary        TEXT NOT NULL,
    why_it_matters TEXT NOT NULL,
    significance   TEXT NOT NULL CHECK (significance IN ('high', 'medium', 'low')),
    folio_areas    TEXT NOT NULL DEFAULT '[]',
    folio_entities TEXT NOT NULL DEFAULT '[]',
    folio_concepts TEXT NOT NULL DEFAULT '[]',
    unresolved     TEXT NOT NULL DEFAULT '[]',
    is_duplicate   INTEGER NOT NULL DEFAULT 0,
    duplicate_of   TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cookies_created_at ON cookies (created_at);

CREATE TABLE IF NOT EXISTS cookie_sources (
    cookie_id TEXT NOT NULL REFERENCES cookies (id),
    source_id TEXT NOT NULL REFERENCES sources (id),
    PRIMARY KEY (cookie_id, source_id)
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date                TEXT PRIMARY KEY,
    total_cookies       INTEGER NOT NULL,
    news_count          INTEGER NOT NULL,
    judgment_count      INTEGER NOT NULL,
    high_significance   TEXT NOT NULL DEFAULT '[]',
    medium_significance TEXT NOT NULL DEFAULT '[]',
    areas_breakdown     TEXT NOT NULL DEFAULT '{}',
    courts_breakdown    TEXT NOT NULL DEFAULT '{}',
    busiest_area        TEXT NOT NULL DEFAULT '',
    unresolved_terms    TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS source_registry (
    zeeker_db  TEXT NOT NULL,
    table_name TEXT NOT NULL,
    pipeline   TEXT NOT NULL CHECK (pipeline IN ('news', 'judgment')),
    license    TEXT NOT NULL,
    watermark  TEXT,
    active     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (zeeker_db, table_name)
);

CREATE TABLE IF NOT EXISTS unresolved_terms (
    term            TEXT PRIMARY KEY,
    first_seen_date TEXT NOT NULL,
    count           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS judgment_meta (
    source_id   TEXT PRIMARY KEY REFERENCES sources (id),
    citation    TEXT NOT NULL,
    court       TEXT,
    judges      TEXT NOT NULL DEFAULT '[]',
    parties     TEXT NOT NULL DEFAULT '[]',
    issues      TEXT NOT NULL DEFAULT '[]',
    legislation TEXT NOT NULL DEFAULT '[]',
    cases_cited TEXT NOT NULL DEFAULT '[]',
    orders      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgment_meta_citation ON judgment_meta (citation);

-- Human-in-the-loop review staging (PRD §4 quality gate).
-- Cookies produced by the pipeline land here first; they are promoted
-- to the live `cookies` table only after Discord approval or 72h
-- auto-approve.  The live site (sitegen) reads only from `cookies`,
-- so pending cookies never appear publicly.
CREATE TABLE IF NOT EXISTS pending_cookies (
    id              TEXT PRIMARY KEY,
    headline        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    why_it_matters  TEXT NOT NULL,
    significance    TEXT NOT NULL CHECK (significance IN ('high', 'medium', 'low')),
    folio_areas     TEXT NOT NULL DEFAULT '[]',
    folio_entities  TEXT NOT NULL DEFAULT '[]',
    folio_concepts  TEXT NOT NULL DEFAULT '[]',
    unresolved      TEXT NOT NULL DEFAULT '[]',
    source_ids      TEXT NOT NULL DEFAULT '[]',
    item_type       TEXT NOT NULL,          -- 'news' | 'judgment'
    source_url      TEXT,                    -- original URL for display in Discord
    created_at      TEXT NOT NULL,            -- when the LLM produced it
    review_status   TEXT NOT NULL DEFAULT 'pending'
                    CHECK (review_status IN ('pending','approved','rejected','regenerating')),
    discord_msg_id  TEXT,                    -- Discord message ID for button mapping
    reject_count    INTEGER NOT NULL DEFAULT 0,
    reject_reason   TEXT,                    -- last rejection reason
    reviewed_at     TEXT,                    -- when approved/rejected
    auto_approved   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_cookies (review_status);
CREATE INDEX IF NOT EXISTS idx_pending_created ON pending_cookies (created_at);

-- Permanent rejection log for kaizen / learning.
-- When a cookie is discarded (after 2nd rejection, or manual purge),
-- it is moved here before being deleted from pending_cookies.  This
-- preserves the full content, the rejection reasons, the unresolved
-- FOLIO terms at time of rejection, and who rejected it — so patterns
-- can be analysed over time (e.g. "cookies from source X are rejected
-- 3x more often" or "unresolved entities correlate with rejections").
CREATE TABLE IF NOT EXISTS rejected_cookies (
    id              TEXT PRIMARY KEY,        -- same cookie ID from pending
    headline        TEXT NOT NULL,
    summary         TEXT NOT NULL,
    why_it_matters  TEXT NOT NULL,
    significance    TEXT NOT NULL,
    folio_areas     TEXT NOT NULL DEFAULT '[]',
    folio_entities  TEXT NOT NULL DEFAULT '[]',
    folio_concepts  TEXT NOT NULL DEFAULT '[]',
    unresolved      TEXT NOT NULL DEFAULT '[]',
    source_ids      TEXT NOT NULL DEFAULT '[]',
    item_type       TEXT NOT NULL,
    source_url      TEXT,
    reject_count    INTEGER NOT NULL,
    reject_reason   TEXT NOT NULL,            -- last rejection reason
    rejected_by     TEXT,                     -- Discord user ID
    rejected_at     TEXT NOT NULL,            -- ISO timestamp
    created_at      TEXT NOT NULL             -- original LLM production time
);
"""


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _dump_refs(refs: list[FolioRef]) -> str:
    return json.dumps([r.model_dump() for r in refs])


def _load_refs(raw: str) -> list[FolioRef]:
    return [FolioRef.model_validate(item) for item in json.loads(raw)]


# ── sources ──────────────────────────────────────────────────────────


def upsert_source(conn: sqlite3.Connection, source: Source) -> None:
    conn.execute(
        """
        INSERT INTO sources (id, source_url, zeeker_url, title, raw_text, date,
                             source_id, item_type, license, token_count, ingested_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            source_url = excluded.source_url,
            zeeker_url = excluded.zeeker_url,
            title = excluded.title,
            raw_text = excluded.raw_text,
            date = excluded.date,
            source_id = excluded.source_id,
            item_type = excluded.item_type,
            license = excluded.license,
            token_count = excluded.token_count,
            ingested_at = excluded.ingested_at
        """,
        (
            source.id,
            source.source_url,
            source.zeeker_url,
            source.title,
            source.raw_text,
            source.date.isoformat(),
            source.source_id,
            source.item_type,
            source.license,
            source.token_count,
            source.ingested_at.isoformat(),
        ),
    )
    conn.commit()


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        source_url=row["source_url"],
        zeeker_url=row["zeeker_url"],
        title=row["title"],
        raw_text=row["raw_text"],
        date=date.fromisoformat(row["date"]),
        source_id=row["source_id"],
        item_type=row["item_type"],
        license=row["license"],
        token_count=row["token_count"],
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
    )


def find_source_by_url(conn: sqlite3.Connection, source_url: str) -> Source | None:
    row = conn.execute(
        "SELECT * FROM sources WHERE source_url = ? ORDER BY ingested_at DESC LIMIT 1",
        (source_url,),
    ).fetchone()
    return _row_to_source(row) if row else None


def get_source(conn: sqlite3.Connection, source_id: str) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _row_to_source(row) if row else None


# ── cookies ──────────────────────────────────────────────────────────


def save_cookie(conn: sqlite3.Connection, cookie: Cookie) -> None:
    """Write the cookie row plus its cookie_sources links."""
    conn.execute(
        """
        INSERT INTO cookies (id, headline, summary, why_it_matters, significance,
                             folio_areas, folio_entities, folio_concepts, unresolved,
                             is_duplicate, duplicate_of, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (id) DO UPDATE SET
            headline = excluded.headline,
            summary = excluded.summary,
            why_it_matters = excluded.why_it_matters,
            significance = excluded.significance,
            folio_areas = excluded.folio_areas,
            folio_entities = excluded.folio_entities,
            folio_concepts = excluded.folio_concepts,
            unresolved = excluded.unresolved,
            is_duplicate = excluded.is_duplicate,
            duplicate_of = excluded.duplicate_of,
            created_at = excluded.created_at
        """,
        (
            cookie.id,
            cookie.headline,
            cookie.summary,
            cookie.why_it_matters,
            cookie.significance,
            _dump_refs(cookie.folio_areas),
            _dump_refs(cookie.folio_entities),
            _dump_refs(cookie.folio_concepts),
            json.dumps(cookie.unresolved),
            int(cookie.is_duplicate),
            cookie.duplicate_of,
            cookie.created_at.isoformat(),
        ),
    )
    conn.execute("DELETE FROM cookie_sources WHERE cookie_id = ?", (cookie.id,))
    conn.executemany(
        "INSERT OR IGNORE INTO cookie_sources (cookie_id, source_id) VALUES (?, ?)",
        [(cookie.id, sid) for sid in cookie.source_ids],
    )
    conn.commit()


def link_cookie_source(conn: sqlite3.Connection, cookie_id: str, source_id: str) -> None:
    """Add a corroborating source to an existing cookie (PRD section 2.1)."""
    conn.execute(
        "INSERT OR IGNORE INTO cookie_sources (cookie_id, source_id) VALUES (?, ?)",
        (cookie_id, source_id),
    )
    conn.commit()


# ── pending cookies (human-in-the-loop review staging) ────────────────
#
# Cookies produced by the pipeline land here first.  They are promoted
# to the live `cookies` table only after Discord approval or 72h
# auto-approve.  The live site (sitegen) reads only from `cookies`.


def save_pending_cookie(
    conn: sqlite3.Connection,
    cookie: Cookie,
    item_type: str,
    source_url: str | None = None,
) -> None:
    """Write a cookie to the pending review staging table.

    Has the same JSON-serialisation conventions as save_cookie — folio
    refs are dumped to JSON, unresolved is a JSON list.  source_ids are
    also JSON-encoded here (pending is not normalised via cookie_sources).
    """
    conn.execute(
        """
        INSERT INTO pending_cookies (id, headline, summary, why_it_matters,
                                      significance, folio_areas, folio_entities,
                                      folio_concepts, unresolved, source_ids,
                                      item_type, source_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            headline = excluded.headline,
            summary = excluded.summary,
            why_it_matters = excluded.why_it_matters,
            significance = excluded.significance,
            folio_areas = excluded.folio_areas,
            folio_entities = excluded.folio_entities,
            folio_concepts = excluded.folio_concepts,
            unresolved = excluded.unresolved,
            source_ids = excluded.source_ids,
            item_type = excluded.item_type,
            source_url = excluded.source_url,
            created_at = excluded.created_at
        """,
        (
            cookie.id,
            cookie.headline,
            cookie.summary,
            cookie.why_it_matters,
            cookie.significance,
            _dump_refs(cookie.folio_areas),
            _dump_refs(cookie.folio_entities),
            _dump_refs(cookie.folio_concepts),
            json.dumps(cookie.unresolved),
            json.dumps(cookie.source_ids),
            item_type,
            source_url,
            cookie.created_at.isoformat(),
        ),
    )
    conn.commit()


def list_pending_cookies(
    conn: sqlite3.Connection,
    status: str = "pending",
    not_posted: bool = False,
) -> list[sqlite3.Row]:
    """Pending cookies by review_status.

    If not_posted is True, only return rows where discord_msg_id IS NULL
    (used by the Discord posting cron — avoids re-posting cookies that
    are already on Discord).
    """
    sql = "SELECT * FROM pending_cookies WHERE review_status = ?"
    params: list = [status]
    if not_posted:
        sql += " AND discord_msg_id IS NULL"
    sql += " ORDER BY created_at"
    return conn.execute(sql, params).fetchall()


def get_pending_cookie(conn: sqlite3.Connection, cookie_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM pending_cookies WHERE id = ?", (cookie_id,)
    ).fetchone()


def set_pending_discord_msg_id(
    conn: sqlite3.Connection, cookie_id: str, discord_msg_id: str
) -> None:
    conn.execute(
        "UPDATE pending_cookies SET discord_msg_id = ? WHERE id = ?",
        (discord_msg_id, cookie_id),
    )
    conn.commit()


def promote_pending_cookie(conn: sqlite3.Connection, cookie_id: str) -> Cookie | None:
    """Move a pending cookie to the live cookies table.

    Copies all cookie fields, creates cookie_sources links, and deletes
    the pending row.  Returns the promoted Cookie, or None if the pending
    row doesn't exist or was already promoted.
    """
    row = get_pending_cookie(conn, cookie_id)
    if row is None:
        return None

    # Check if already in live cookies (idempotent — avoid double insert)
    existing = conn.execute(
        "SELECT 1 FROM cookies WHERE id = ?", (cookie_id,)
    ).fetchone()
    if existing is not None:
        # Already promoted; just clean up the pending row.
        conn.execute("DELETE FROM pending_cookies WHERE id = ?", (cookie_id,))
        conn.commit()
        return get_cookie(conn, cookie_id)

    cookie = Cookie(
        id=row["id"],
        source_ids=json.loads(row["source_ids"]),
        headline=row["headline"],
        summary=row["summary"],
        why_it_matters=row["why_it_matters"],
        significance=row["significance"],
        folio_areas=_load_refs(row["folio_areas"]),
        folio_entities=_load_refs(row["folio_entities"]),
        folio_concepts=_load_refs(row["folio_concepts"]),
        unresolved=json.loads(row["unresolved"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )
    # Insert into live cookies + source links.
    conn.execute(
        """
        INSERT INTO cookies (id, headline, summary, why_it_matters, significance,
                             folio_areas, folio_entities, folio_concepts, unresolved,
                             is_duplicate, duplicate_of, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, ?)
        """,
        (
            cookie.id,
            cookie.headline,
            cookie.summary,
            cookie.why_it_matters,
            cookie.significance,
            _dump_refs(cookie.folio_areas),
            _dump_refs(cookie.folio_entities),
            _dump_refs(cookie.folio_concepts),
            json.dumps(cookie.unresolved),
            cookie.created_at.isoformat(),
        ),
    )
    conn.executemany(
        "INSERT OR IGNORE INTO cookie_sources (cookie_id, source_id) VALUES (?, ?)",
        [(cookie.id, sid) for sid in cookie.source_ids],
    )
    # Mark as approved, then delete the pending row.
    conn.execute(
        "UPDATE pending_cookies SET review_status = 'approved', reviewed_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), cookie_id),
    )
    conn.execute("DELETE FROM pending_cookies WHERE id = ?", (cookie_id,))
    conn.commit()
    return cookie


def reject_pending_cookie(
    conn: sqlite3.Connection,
    cookie_id: str,
    reason: str,
    rejected_by: str | None = None,
    max_rejects: int = 2,
) -> str:
    """Reject a pending cookie.  Returns the new review_status.

    On the first rejection: sets review_status='regenerating', increments
    reject_count, stores the reason, clears discord_msg_id (so the
    regenerated cookie gets re-posted for review).

    On the final rejection (reject_count >= max_rejects): moves the
    cookie to rejected_cookies (permanent log) and deletes it from
    pending_cookies.
    """
    row = get_pending_cookie(conn, cookie_id)
    if row is None:
        raise KeyError(f"no pending cookie with id {cookie_id!r}")

    new_count = row["reject_count"] + 1
    now = datetime.now(timezone.utc).isoformat()

    if new_count >= max_rejects:
        # Final rejection — archive to rejected_cookies, then delete.
        conn.execute(
            """
            INSERT INTO rejected_cookies (id, headline, summary, why_it_matters,
                                           significance, folio_areas, folio_entities,
                                           folio_concepts, unresolved, source_ids,
                                           item_type, source_url, reject_count,
                                           reject_reason, rejected_by, rejected_at,
                                           created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["headline"],
                row["summary"],
                row["why_it_matters"],
                row["significance"],
                row["folio_areas"],
                row["folio_entities"],
                row["folio_concepts"],
                row["unresolved"],
                row["source_ids"],
                row["item_type"],
                row["source_url"],
                new_count,
                reason,
                rejected_by,
                now,
                row["created_at"],
            ),
        )
        conn.execute("DELETE FROM pending_cookies WHERE id = ?", (cookie_id,))
        conn.commit()
        return "rejected"

    # First rejection — re-queue for regeneration.
    conn.execute(
        """
        UPDATE pending_cookies
        SET review_status = 'regenerating',
            reject_count = ?,
            reject_reason = ?,
            discord_msg_id = NULL
        WHERE id = ?
        """,
        (new_count, reason, cookie_id),
    )
    conn.commit()
    return "regenerating"


def update_pending_cookie_text(
    conn: sqlite3.Connection,
    cookie_id: str,
    headline: str | None = None,
    summary: str | None = None,
    why_it_matters: str | None = None,
) -> None:
    """Update editable text fields on a pending cookie (from the Edit button)."""
    updates: list[str] = []
    params: list = []
    if headline is not None:
        updates.append("headline = ?")
        params.append(headline)
    if summary is not None:
        updates.append("summary = ?")
        params.append(summary)
    if why_it_matters is not None:
        updates.append("why_it_matters = ?")
        params.append(why_it_matters)
    if not updates:
        return
    params.append(cookie_id)
    conn.execute(
        f"UPDATE pending_cookies SET {', '.join(updates)} WHERE id = ?",
        params,
    )
    conn.commit()


def list_auto_approvable(
    conn: sqlite3.Connection, max_age_hours: int = 72
) -> list[sqlite3.Row]:
    """Pending cookies older than max_age_hours, still in 'pending' status."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    ).isoformat()
    return conn.execute(
        """
        SELECT * FROM pending_cookies
        WHERE review_status = 'pending' AND created_at < ?
        ORDER BY created_at
        """,
        (cutoff,),
    ).fetchall()


def auto_approve_pending(
    conn: sqlite3.Connection, max_age_hours: int = 72
) -> list[str]:
    """Promote all pending cookies older than max_age_hours to live cookies.

    Returns the list of promoted cookie IDs.  Sets auto_approved=1 on
    each before promoting (for audit tracking — the fact that it was
    auto-approved is lost when the pending row is deleted, so we log
    this separately if needed).
    """
    rows = list_auto_approvable(conn, max_age_hours)
    promoted: list[str] = []
    for row in rows:
        # Mark as auto-approved for any audit log before promoting.
        conn.execute(
            "UPDATE pending_cookies SET auto_approved = 1 WHERE id = ?",
            (row["id"],),
        )
        conn.commit()
        cookie = promote_pending_cookie(conn, row["id"])
        if cookie is not None:
            promoted.append(cookie.id)
    return promoted


def list_regenerating(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Pending cookies waiting for LLM regeneration after rejection."""
    return conn.execute(
        "SELECT * FROM pending_cookies WHERE review_status = 'regenerating' ORDER BY created_at"
    ).fetchall()


def count_pending(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts by review_status — for watchdog / dashboard reporting."""
    rows = conn.execute(
        "SELECT review_status, COUNT(*) AS n FROM pending_cookies GROUP BY review_status"
    ).fetchall()
    return {row["review_status"]: row["n"] for row in rows}


# ── rejected cookies log (kaizen / learning) ───────────────────────────


def list_rejected_cookies(
    conn: sqlite3.Connection, limit: int = 100
) -> list[sqlite3.Row]:
    """Rejected cookies, newest rejection first."""
    return conn.execute(
        "SELECT * FROM rejected_cookies ORDER BY rejected_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


def rejected_cookie_stats(conn: sqlite3.Connection) -> dict:
    """Aggregate stats for kaizen analysis.

    Returns: total rejected, by significance, by item_type, top rejection
    reasons, and whether unresolved FOLIO terms correlate with rejections.
    """
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM rejected_cookies"
    ).fetchone()["n"]
    by_sig = {
        row["significance"]: row["n"]
        for row in conn.execute(
            "SELECT significance, COUNT(*) AS n FROM rejected_cookies GROUP BY significance"
        )
    }
    by_type = {
        row["item_type"]: row["n"]
        for row in conn.execute(
            "SELECT item_type, COUNT(*) AS n FROM rejected_cookies GROUP BY item_type"
        )
    }
    # How many rejected cookies had unresolved FOLIO terms?
    had_unresolved = conn.execute(
        "SELECT COUNT(*) AS n FROM rejected_cookies WHERE unresolved != '[]'"
    ).fetchone()["n"]
    return {
        "total": total,
        "by_significance": by_sig,
        "by_item_type": by_type,
        "had_unresolved_terms": had_unresolved,
        "pct_with_unresolved": round(had_unresolved / total * 100, 1) if total else 0,
    }


def _row_to_cookie(conn: sqlite3.Connection, row: sqlite3.Row) -> Cookie:
    source_ids = [
        r["source_id"]
        for r in conn.execute(
            "SELECT source_id FROM cookie_sources WHERE cookie_id = ? ORDER BY source_id",
            (row["id"],),
        )
    ]
    return Cookie(
        id=row["id"],
        source_ids=source_ids,
        headline=row["headline"],
        summary=row["summary"],
        why_it_matters=row["why_it_matters"],
        significance=row["significance"],
        folio_areas=_load_refs(row["folio_areas"]),
        folio_entities=_load_refs(row["folio_entities"]),
        folio_concepts=_load_refs(row["folio_concepts"]),
        unresolved=json.loads(row["unresolved"]),
        is_duplicate=bool(row["is_duplicate"]),
        duplicate_of=row["duplicate_of"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def get_cookie(conn: sqlite3.Connection, cookie_id: str) -> Cookie | None:
    row = conn.execute("SELECT * FROM cookies WHERE id = ?", (cookie_id,)).fetchone()
    return _row_to_cookie(conn, row) if row else None


def all_cookies(conn: sqlite3.Connection) -> list[Cookie]:
    """Every cookie, oldest first (used by maintenance tasks like backfills)."""
    rows = conn.execute("SELECT * FROM cookies ORDER BY created_at").fetchall()
    return [_row_to_cookie(conn, row) for row in rows]


def update_cookie_areas(
    conn: sqlite3.Connection, cookie_id: str, folio_areas: list[FolioRef]
) -> None:
    """Replace just a cookie's folio_areas, leaving every other field intact."""
    conn.execute(
        "UPDATE cookies SET folio_areas = ? WHERE id = ?",
        (_dump_refs(folio_areas), cookie_id),
    )
    conn.commit()


def find_recent_cookies(
    conn: sqlite3.Connection, lookback_days: int, as_of: date | None = None
) -> list[Cookie]:
    """Cookies created within the lookback window, for dedup checks."""
    end = as_of or date.today()
    cutoff = (end - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT * FROM cookies WHERE created_at >= ? ORDER BY created_at DESC",
        (cutoff,),
    ).fetchall()
    return [_row_to_cookie(conn, row) for row in rows]


# ── source registry / watermarks ─────────────────────────────────────


def upsert_registry_entry(conn: sqlite3.Connection, entry: SourceRegistryEntry) -> None:
    conn.execute(
        """
        INSERT INTO source_registry (zeeker_db, table_name, pipeline, license, watermark, active)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (zeeker_db, table_name) DO UPDATE SET
            pipeline = excluded.pipeline,
            license = excluded.license,
            watermark = excluded.watermark,
            active = excluded.active
        """,
        (
            entry.zeeker_db,
            entry.table,
            entry.pipeline,
            entry.license,
            entry.watermark,
            int(entry.active),
        ),
    )
    conn.commit()


def _row_to_registry_entry(row: sqlite3.Row) -> SourceRegistryEntry:
    return SourceRegistryEntry(
        zeeker_db=row["zeeker_db"],
        table=row["table_name"],
        pipeline=row["pipeline"],
        license=row["license"],
        watermark=row["watermark"],
        active=bool(row["active"]),
    )


def list_registry(
    conn: sqlite3.Connection, active_only: bool = False
) -> list[SourceRegistryEntry]:
    sql = "SELECT * FROM source_registry"
    if active_only:
        sql += " WHERE active = 1"
    sql += " ORDER BY zeeker_db, table_name"
    return [_row_to_registry_entry(row) for row in conn.execute(sql)]


def get_watermark(conn: sqlite3.Connection, zeeker_db: str, table: str) -> str | None:
    row = conn.execute(
        "SELECT watermark FROM source_registry WHERE zeeker_db = ? AND table_name = ?",
        (zeeker_db, table),
    ).fetchone()
    return row["watermark"] if row else None


def set_watermark(
    conn: sqlite3.Connection, zeeker_db: str, table: str, watermark: str
) -> None:
    cur = conn.execute(
        "UPDATE source_registry SET watermark = ? WHERE zeeker_db = ? AND table_name = ?",
        (watermark, zeeker_db, table),
    )
    conn.commit()
    if cur.rowcount == 0:
        raise KeyError(f"no registry entry for ({zeeker_db}, {table})")


# ── unresolved terms ─────────────────────────────────────────────────


def record_unresolved_terms(
    conn: sqlite3.Connection, terms: list[str], seen: date | None = None
) -> None:
    """Tally unresolved FOLIO terms for review (PRD section 4.3)."""
    seen_iso = (seen or date.today()).isoformat()
    conn.executemany(
        """
        INSERT INTO unresolved_terms (term, first_seen_date, count)
        VALUES (?, ?, 1)
        ON CONFLICT (term) DO UPDATE SET count = count + 1
        """,
        [(term, seen_iso) for term in terms],
    )
    conn.commit()


def list_unresolved_terms(conn: sqlite3.Connection) -> list[tuple[str, date, int]]:
    return [
        (row["term"], date.fromisoformat(row["first_seen_date"]), row["count"])
        for row in conn.execute(
            "SELECT * FROM unresolved_terms ORDER BY count DESC, term"
        )
    ]


# ── daily stats ──────────────────────────────────────────────────────


def save_daily_stats(conn: sqlite3.Connection, stats: DailyStats) -> None:
    conn.execute(
        """
        INSERT INTO daily_stats (date, total_cookies, news_count, judgment_count,
                                 high_significance, medium_significance,
                                 areas_breakdown, courts_breakdown,
                                 busiest_area, unresolved_terms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (date) DO UPDATE SET
            total_cookies = excluded.total_cookies,
            news_count = excluded.news_count,
            judgment_count = excluded.judgment_count,
            high_significance = excluded.high_significance,
            medium_significance = excluded.medium_significance,
            areas_breakdown = excluded.areas_breakdown,
            courts_breakdown = excluded.courts_breakdown,
            busiest_area = excluded.busiest_area,
            unresolved_terms = excluded.unresolved_terms
        """,
        (
            stats.date.isoformat(),
            stats.total_cookies,
            stats.news_count,
            stats.judgment_count,
            json.dumps(stats.high_significance),
            json.dumps(stats.medium_significance),
            json.dumps(stats.areas_breakdown),
            json.dumps(stats.courts_breakdown),
            stats.busiest_area,
            json.dumps(stats.unresolved_terms),
        ),
    )
    conn.commit()


def get_daily_stats(conn: sqlite3.Connection, day: date) -> DailyStats | None:
    row = conn.execute(
        "SELECT * FROM daily_stats WHERE date = ?", (day.isoformat(),)
    ).fetchone()
    if row is None:
        return None
    return DailyStats(
        date=date.fromisoformat(row["date"]),
        total_cookies=row["total_cookies"],
        news_count=row["news_count"],
        judgment_count=row["judgment_count"],
        high_significance=json.loads(row["high_significance"]),
        medium_significance=json.loads(row["medium_significance"]),
        areas_breakdown=json.loads(row["areas_breakdown"]),
        courts_breakdown=json.loads(row["courts_breakdown"]),
        busiest_area=row["busiest_area"],
        unresolved_terms=json.loads(row["unresolved_terms"]),
    )


# ── site read queries (sitegen / feed) ───────────────────────────────


# A cookie is filed under its PUBLICATION date — the earliest document date
# across its sources (judgment decision_date, article published_date), not the
# date we processed it. Sourceless cookies fall back to their processing date so
# they still land on a page. Source dates are ISO 'YYYY-MM-DD' TEXT, so MIN()
# and string comparison sort chronologically.
_PUB_DATE_SQL = (
    "COALESCE("
    "(SELECT MIN(s.date) FROM sources s "
    "JOIN cookie_sources cs ON cs.source_id = s.id "
    "WHERE cs.cookie_id = cookies.id), "
    "date(cookies.created_at))"
)


def list_cookie_dates(conn: sqlite3.Connection) -> list[str]:
    """Distinct publication days that have cookies, newest first."""
    return [
        row["day"]
        for row in conn.execute(
            f"SELECT DISTINCT {_PUB_DATE_SQL} AS day FROM cookies ORDER BY day DESC"
        )
    ]


def cookies_for_date(conn: sqlite3.Connection, day: date) -> list[Cookie]:
    """All cookies published on the given day, oldest first (by processing time).

    "Published" = the earliest source document date (see _PUB_DATE_SQL).
    Includes duplicate-flagged cookies; filtering is left to the caller.
    """
    rows = conn.execute(
        f"SELECT * FROM cookies WHERE {_PUB_DATE_SQL} = ? ORDER BY created_at, id",
        (day.isoformat(),),
    ).fetchall()
    return [_row_to_cookie(conn, row) for row in rows]


def cookies_for_week(conn: sqlite3.Connection, monday: date) -> list[Cookie]:
    """All cookies published in the 7-day week beginning `monday`, oldest first.

    Day grouping uses publication date (see _PUB_DATE_SQL); the upper bound is
    exclusive. Includes duplicate-flagged cookies; filtering is left to the
    caller.
    """
    start = monday.isoformat()
    end = (monday + timedelta(days=7)).isoformat()  # exclusive
    rows = conn.execute(
        f"SELECT * FROM cookies WHERE {_PUB_DATE_SQL} >= ? AND {_PUB_DATE_SQL} < ? "
        "ORDER BY created_at, id",
        (start, end),
    ).fetchall()
    return [_row_to_cookie(conn, row) for row in rows]


def sources_for_cookies(
    conn: sqlite3.Connection, cookie_ids: list[str]
) -> dict[str, list[Source]]:
    """Map cookie id -> its Sources via the cookie_sources join.

    Source.source_url is the ORIGINAL document URL (never a Zeeker URL);
    use it for all outbound links (PRD §2.5). Every requested id is a key,
    even when it has no sources.
    """
    result: dict[str, list[Source]] = {cid: [] for cid in cookie_ids}
    if not cookie_ids:
        return result
    placeholders = ", ".join("?" for _ in cookie_ids)
    rows = conn.execute(
        f"""
        SELECT cs.cookie_id AS link_cookie_id, s.*
        FROM sources s
        JOIN cookie_sources cs ON cs.source_id = s.id
        WHERE cs.cookie_id IN ({placeholders})
        ORDER BY cs.cookie_id, s.ingested_at, s.id
        """,
        list(cookie_ids),
    ).fetchall()
    for row in rows:
        result[row["link_cookie_id"]].append(_row_to_source(row))
    return result


def latest_unresolved_terms(conn: sqlite3.Connection, day: date) -> list[str]:
    """Unresolved FOLIO terms first seen on the given day, most frequent first."""
    return [
        row["term"]
        for row in conn.execute(
            """
            SELECT term FROM unresolved_terms
            WHERE first_seen_date = ?
            ORDER BY count DESC, term
            """,
            (day.isoformat(),),
        )
    ]


def compute_daily_stats(conn: sqlite3.Connection, day: date) -> DailyStats:
    """Compute stats over cookies created on the given date."""
    day_iso = day.isoformat()
    rows = conn.execute(
        "SELECT * FROM cookies WHERE substr(created_at, 1, 10) = ? ORDER BY created_at",
        (day_iso,),
    ).fetchall()
    cookies = [_row_to_cookie(conn, row) for row in rows]

    news_count = 0
    judgment_count = 0
    high: list[str] = []
    medium: list[str] = []
    areas: dict[str, int] = {}
    unresolved: list[str] = []

    for cookie in cookies:
        item_types = {
            r["item_type"]
            for r in conn.execute(
                """
                SELECT s.item_type FROM sources s
                JOIN cookie_sources cs ON cs.source_id = s.id
                WHERE cs.cookie_id = ?
                """,
                (cookie.id,),
            )
        }
        if "news" in item_types:
            news_count += 1
        if "judgment" in item_types:
            judgment_count += 1
        if cookie.significance == "high":
            high.append(cookie.id)
        elif cookie.significance == "medium":
            medium.append(cookie.id)
        for ref in cookie.folio_areas:
            areas[ref.preferred_label] = areas.get(ref.preferred_label, 0) + 1
        for term in cookie.unresolved:
            if term not in unresolved:
                unresolved.append(term)

    busiest_area = max(areas, key=lambda label: areas[label]) if areas else ""

    return DailyStats(
        date=day,
        total_cookies=len(cookies),
        news_count=news_count,
        judgment_count=judgment_count,
        high_significance=high,
        medium_significance=medium,
        areas_breakdown=areas,
        courts_breakdown={},
        busiest_area=busiest_area,
        unresolved_terms=unresolved,
    )


# ── judgment metadata (PRD section 4.2) ──────────────────────────────


def _normalise_citation(citation: str) -> str:
    """Lowercase + collapse all whitespace runs, for exact citation matching."""
    return " ".join(citation.split()).lower()


def save_judgment_meta(
    conn: sqlite3.Connection, source_id: str, meta: JudgmentMeta
) -> None:
    """Upsert the structured judgment metadata for a Source row."""
    conn.execute(
        """
        INSERT INTO judgment_meta (source_id, citation, court, judges, parties,
                                   issues, legislation, cases_cited, orders, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (source_id) DO UPDATE SET
            citation = excluded.citation,
            court = excluded.court,
            judges = excluded.judges,
            parties = excluded.parties,
            issues = excluded.issues,
            legislation = excluded.legislation,
            cases_cited = excluded.cases_cited,
            orders = excluded.orders
        """,
        (
            source_id,
            meta.citation,
            json.dumps(meta.court.model_dump()) if meta.court else None,
            json.dumps(meta.judges),
            json.dumps(meta.parties),
            json.dumps([issue.model_dump() for issue in meta.issues]),
            _dump_refs(meta.legislation),
            json.dumps([case.model_dump() for case in meta.cases_cited]),
            meta.orders,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


def get_judgment_meta(conn: sqlite3.Connection, source_id: str) -> JudgmentMeta | None:
    row = conn.execute(
        "SELECT * FROM judgment_meta WHERE source_id = ?", (source_id,)
    ).fetchone()
    if row is None:
        return None
    return JudgmentMeta(
        source_id=row["source_id"],
        citation=row["citation"],
        court=FolioRef.model_validate(json.loads(row["court"])) if row["court"] else None,
        judges=json.loads(row["judges"]),
        parties=json.loads(row["parties"]),
        issues=[JudgmentIssue.model_validate(item) for item in json.loads(row["issues"])],
        legislation=_load_refs(row["legislation"]),
        cases_cited=[
            CaseCitation.model_validate(item) for item in json.loads(row["cases_cited"])
        ],
        orders=row["orders"],
    )


def find_source_by_citation(conn: sqlite3.Connection, citation: str) -> Source | None:
    """Find the Source for a neutral citation (exact, case/whitespace-insensitive).

    Powers CaseCitation.internal_ref cross-linking (pseudocode section 3 step 5).
    """
    target = _normalise_citation(citation)
    if not target:
        return None
    for row in conn.execute("SELECT source_id, citation FROM judgment_meta"):
        if _normalise_citation(row["citation"]) == target:
            return get_source(conn, row["source_id"])
    return None


def list_judgment_citations(conn: sqlite3.Connection) -> dict[str, str]:
    """Map normalised-lowercase citation -> source id, for batch cross-linking."""
    return {
        _normalise_citation(row["citation"]): row["source_id"]
        for row in conn.execute(
            "SELECT citation, source_id FROM judgment_meta ORDER BY created_at, source_id"
        )
        if _normalise_citation(row["citation"])
    }
