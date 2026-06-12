"""News enrichment pipeline and per-source run loop (PRD sections 4.1, 8)."""

import sqlite3
from dataclasses import dataclass, field

import httpx

from sg_law_cookies import db
from sg_law_cookies.extraction import DEFAULT_MODEL
from sg_law_cookies.folio import resolve_topic
from sg_law_cookies.judgment import process_judgment
from sg_law_cookies.llm import as_backend
from sg_law_cookies.models import (
    Cookie,
    RawItem,
    Source,
    SourceRegistryEntry,
    TopicExtraction,
)
from sg_law_cookies.zeeker import ZeekerClient, item_type_for, timestamp_column

DEDUP_LOOKBACK_DAYS = 7


class PipelineError(RuntimeError):
    pass


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _cookies_for_source(conn: sqlite3.Connection, source_id: str) -> list[Cookie]:
    rows = conn.execute(
        "SELECT cookie_id FROM cookie_sources WHERE source_id = ?", (source_id,)
    ).fetchall()
    cookies = (db.get_cookie(conn, row["cookie_id"]) for row in rows)
    return [cookie for cookie in cookies if cookie is not None]


def _find_duplicate(topic: TopicExtraction, recent: list[Cookie]) -> Cookie | None:
    headline = topic.headline.strip().lower()
    for cookie in recent:
        if cookie.headline.strip().lower() == headline:
            return cookie
    return None


def process_news(
    raw_item: RawItem,
    conn: sqlite3.Connection,
    llm: object,
    folio_client: httpx.Client,
    model: str = DEFAULT_MODEL,
) -> list[Cookie]:
    """Extract -> FOLIO resolution -> dedup -> store (PRD section 4.1).

    `llm` is an LLMBackend (Anthropic or Ollama) or a bare Anthropic client.
    Returns the cookies linked to this item. If the source URL was already
    ingested, returns the existing cookies without a new LLM call.
    """
    existing = db.find_source_by_url(conn, raw_item.source_url)
    if existing is not None:
        return _cookies_for_source(conn, existing.id)

    topics = as_backend(llm, model).extract_topics(raw_item)
    for topic in topics:
        resolve_topic(topic, folio_client)

    source = Source(
        source_url=raw_item.source_url,
        zeeker_url=raw_item.zeeker_url,
        title=raw_item.title,
        raw_text=raw_item.raw_text,
        date=raw_item.date,
        source_id=raw_item.source_id,
        item_type=raw_item.item_type,
        license=raw_item.license,
        token_count=_estimate_tokens(raw_item.raw_text),
    )
    db.upsert_source(conn, source)

    recent = [
        cookie
        for cookie in db.find_recent_cookies(conn, DEDUP_LOOKBACK_DAYS)
        if not cookie.is_duplicate
    ]
    cookies: list[Cookie] = []
    unresolved: list[str] = []
    for topic in topics:
        original = _find_duplicate(topic, recent)
        cookie = Cookie(
            source_ids=[source.id],
            headline=topic.headline,
            summary=topic.summary,
            why_it_matters=topic.why_it_matters,
            significance=topic.significance,
            folio_areas=topic.folio_areas,
            folio_entities=topic.folio_entities,
            folio_concepts=topic.folio_concepts,
            unresolved=topic.unresolved,
            is_duplicate=original is not None,
            duplicate_of=original.id if original else None,
        )
        db.save_cookie(conn, cookie)
        if original is not None:
            # Duplicates are flagged, not deleted; the new source corroborates
            # the original cookie (PRD sections 2.1, 4.1 step 3).
            db.link_cookie_source(conn, original.id, source.id)
        unresolved.extend(topic.unresolved)
        cookies.append(cookie)

    if unresolved:
        db.record_unresolved_terms(conn, unresolved)
    return cookies


@dataclass
class RunResult:
    source: str
    processed: int = 0
    skipped: int = 0  # rows with no usable text (judgment tables)
    cookies: list[Cookie] = field(default_factory=list)
    watermark: str | None = None
    dry_run_items: list[RawItem] = field(default_factory=list)


def run_source(
    conn: sqlite3.Connection,
    zeeker_client: ZeekerClient,
    llm: object | None,
    folio_client: httpx.Client | None,
    source: str,
    limit: int = 100,
    dry_run: bool = False,
    model: str = DEFAULT_MODEL,
) -> RunResult:
    """Fetch rows newer than the watermark and process each through the
    news or judgment pipeline. The watermark advances only after a
    successful store (or after a row is skipped for carrying no text)."""
    zeeker_db, _, table = source.partition("/")
    if not zeeker_db or not table:
        raise PipelineError(f"source must be '<database>/<table>', got {source!r}")

    # Registry gate: an entry that exists but is inactive refuses to run.
    # A missing entry is grandfathered (implicit routing by table name),
    # so pre-registry sources like sglawwatch/headlines keep working.
    entry = next(
        (
            e
            for e in db.list_registry(conn)
            if e.zeeker_db == zeeker_db and e.table == table
        ),
        None,
    )
    if entry is not None and not entry.active:
        raise PipelineError(
            f"{source} is registered but inactive; "
            f"run 'cookies activate {source}' to enable it"
        )
    pipeline = entry.pipeline if entry is not None else item_type_for(table)

    since = db.get_watermark(conn, zeeker_db, table)
    rows = zeeker_client.fetch_new_rows(zeeker_db, table, since=since, limit=limit)
    result = RunResult(source=source, watermark=since)
    ts_col = timestamp_column(zeeker_db, table)

    if not dry_run and entry is None:
        db.upsert_registry_entry(
            conn,
            SourceRegistryEntry(
                zeeker_db=zeeker_db,
                table=table,
                pipeline=pipeline,
                license=zeeker_client.license_for(zeeker_db),
            ),
        )

    for row in rows:
        raw_item = zeeker_client.to_raw_item(zeeker_db, table, row)
        watermark = row.get(ts_col)
        if raw_item is None:
            # No usable text (judgment rows): nothing to store, but the
            # row is handled — advance the watermark past it.
            result.skipped += 1
            if not dry_run and watermark is not None:
                db.set_watermark(conn, zeeker_db, table, str(watermark))
                result.watermark = str(watermark)
            continue
        if dry_run:
            result.dry_run_items.append(raw_item)
            continue
        if pipeline == "judgment":
            result.cookies.extend(
                process_judgment(raw_item, conn, llm, folio_client, model=model)
            )
        else:
            result.cookies.extend(
                process_news(raw_item, conn, llm, folio_client, model=model)
            )
        result.processed += 1
        if watermark is not None:
            db.set_watermark(conn, zeeker_db, table, str(watermark))
            result.watermark = str(watermark)
    return result
