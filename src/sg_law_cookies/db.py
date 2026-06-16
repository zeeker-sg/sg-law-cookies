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
