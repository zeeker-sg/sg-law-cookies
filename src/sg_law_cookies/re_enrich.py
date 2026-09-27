"""Re-enrich cookies that have garbage FOLIO tags.

Before the word-overlap rescoring fix (PR #7), every cookie received
semantically unrelated FOLIO tags — US state courts, industry codes,
currency names — at confidence 0.9.  This module re-extracts topics from
the original source text and re-resolves them through the fixed pipeline,
then updates the cookie's folio_areas, folio_entities, folio_concepts,
and unresolved fields in-place.

Only cookies with detected garbage tags (or empty areas) are re-processed.
Cookies that already have clean tags are left untouched.

Usage::

    python -m sg_law_cookies.re_enrich --dry-run   # preview
    python -m sg_law_cookies.re_enrich              # apply
    python -m sg_law_cookies.re_enrich --limit 10   # small batch
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import httpx

from sg_law_cookies import db
from sg_law_cookies.folio import resolve_topic
from sg_law_cookies.llm import LLMBackend
from sg_law_cookies.models import FolioRef, RawItem, TopicExtraction

logger = logging.getLogger(__name__)

# Branches that indicate a garbage tag from the pre-fix pipeline.
# Any entity or concept with one of these branches at confidence >= 0.85
# is a junk match from the FOLIO API's score-90 floor.
_GARBAGE_BRANCHES: frozenset[str] = frozenset({
    "industry_and_market",
    "forums_and_venues",
    "currency",
    "standards_compatibility",
    "actor_/_player",
})

# sg_local and sg_local_area are always legitimate (local mapping tables).
# areas_of_law, legal_authorities, legal_entity, objectives, service,
# document_/_artifact, engagement_attributes, asset_type are legitimate
# FOLIO branches — but only when the tag actually has word overlap, which
# the fix now ensures. Old tags in these branches may still be garbage
# if they were assigned pre-fix; we detect those by checking whether the
# label shares any content word with the cookie's headline/summary.


@dataclass
class ReEnrichReport:
    total: int = 0
    considered: int = 0       # cookies that had garbage/empty tags
    re_extracted: int = 0     # LLM extraction succeeded
    failed: int = 0           # LLM extraction failed
    changed: int = 0          # folio_* fields actually changed
    skipped_clean: int = 0    # cookies that were already clean
    skipped_no_source: int = 0  # cookies with no source text
    changes: list[dict] = field(default_factory=list)


def _has_garbage_tags(cookie) -> bool:
    """Check if a cookie has garbage FOLIO tags that need re-enrichment."""
    # Empty areas — might have areas that went to unresolved
    if not cookie.folio_areas:
        return True

    # Garbage entities
    for e in cookie.folio_entities:
        if e.branch in _GARBAGE_BRANCHES and e.confidence >= 0.85:
            return True
        if e.branch == "unresolved" and e.confidence == 0.0:
            continue  # unresolved is fine — just means no match

    # Garbage concepts
    for c in cookie.folio_concepts:
        if c.branch in _GARBAGE_BRANCHES and c.confidence >= 0.85:
            return True

    return False


def _source_for_cookie(conn: sqlite3.Connection, cookie_id: str) -> dict | None:
    """Get the source row linked to a cookie."""
    row = conn.execute(
        "SELECT s.* FROM sources s "
        "JOIN cookie_sources cs ON s.id = cs.source_id "
        "WHERE cs.cookie_id = ?",
        (cookie_id,),
    ).fetchone()
    return dict(row) if row else None


def _source_to_raw_item(source: dict) -> RawItem:
    """Convert a stored source row to a RawItem for re-extraction."""
    return RawItem(
        source_url=source["source_url"],
        zeeker_url=source["zeeker_url"],
        title=source["title"],
        raw_text=source["raw_text"],
        date=date.fromisoformat(source["date"]),
        source_id=source["source_id"],
        item_type=source["item_type"],
        license=source["license"],
    )


def _update_cookie_tags(
    conn: sqlite3.Connection,
    cookie_id: str,
    topic: TopicExtraction,
) -> bool:
    """Update a cookie's folio_* fields from a re-resolved topic.

    Returns True if any field changed.
    """
    old = conn.execute(
        "SELECT folio_areas, folio_entities, folio_concepts, unresolved "
        "FROM cookies WHERE id = ?",
        (cookie_id,),
    ).fetchone()

    new_areas = db._dump_refs(topic.folio_areas)
    new_entities = db._dump_refs(topic.folio_entities)
    new_concepts = db._dump_refs(topic.folio_concepts)
    new_unresolved = json.dumps(topic.unresolved)

    if (
        old["folio_areas"] == new_areas
        and old["folio_entities"] == new_entities
        and old["folio_concepts"] == new_concepts
        and old["unresolved"] == new_unresolved
    ):
        return False

    conn.execute(
        "UPDATE cookies SET "
        "  folio_areas = ?, folio_entities = ?, "
        "  folio_concepts = ?, unresolved = ? "
        "WHERE id = ?",
        (new_areas, new_entities, new_concepts, new_unresolved, cookie_id),
    )
    conn.commit()
    return True


def _match_topic_to_cookie(
    topics: list[TopicExtraction], cookie
) -> TopicExtraction | None:
    """Match a re-extracted topic to the stored cookie by headline.

    The LLM may extract multiple topics from one source. We match by
    the closest headline (case-insensitive, stripped). If no match,
    return the first topic (the source only had one cookie).
    """
    if len(topics) == 1:
        return topics[0]

    cookie_headline = cookie.headline.strip().lower()
    for t in topics:
        if t.headline.strip().lower() == cookie_headline:
            return t

    # Fuzzy: try first 60 chars
    for t in topics:
        if t.headline.strip().lower()[:60] == cookie_headline[:60]:
            return t

    # If only one cookie came from this source, take the first topic
    return topics[0] if topics else None


def re_enrich(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    folio_client: httpx.Client,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    progress=None,
) -> ReEnrichReport:
    """Re-extract and re-resolve FOLIO tags for cookies with garbage tags.

    Args:
        conn: SQLite connection to the cookies DB.
        backend: LLM backend for topic extraction.
        folio_client: HTTP client for FOLIO API calls.
        dry_run: If True, don't write changes to the DB.
        limit: Maximum number of cookies to process (None = all).
        progress: Optional callback(i, total, cookie, status, detail).
    """
    report = ReEnrichReport()
    cookies = db.all_cookies(conn)
    report.total = len(cookies)

    for i, cookie in enumerate(cookies):
        if limit is not None and report.considered >= limit:
            break

        if not _has_garbage_tags(cookie):
            report.skipped_clean += 1
            continue

        # Get the source text
        source = _source_for_cookie(conn, cookie.id)
        if source is None or not source.get("raw_text"):
            report.skipped_no_source += 1
            if progress is not None:
                progress(i, len(cookies), cookie, "NO_SOURCE", "")
            continue

        report.considered += 1

        # Re-extract topics from source text
        try:
            raw_item = _source_to_raw_item(source)
            topics = backend.extract_topics(raw_item)
        except Exception as exc:
            report.failed += 1
            logger.warning("Extraction failed for %s: %s", cookie.id, exc)
            if progress is not None:
                progress(i, len(cookies), cookie, "FAILED", str(exc)[:80])
            continue

        # Match the right topic to this cookie
        topic = _match_topic_to_cookie(topics, cookie)
        if topic is None:
            report.failed += 1
            if progress is not None:
                progress(i, len(cookies), cookie, "NO_MATCH", "")
            continue

        # Re-resolve FOLIO tags through the fixed pipeline
        resolve_topic(topic, folio_client)

        report.re_extracted += 1

        # Check if anything changed
        if dry_run:
            old_areas = [r.preferred_label for r in cookie.folio_areas]
            new_areas = [r.preferred_label for r in topic.folio_areas]
            old_entities = [r.preferred_label for r in cookie.folio_entities]
            new_entities = [r.preferred_label for r in topic.folio_entities]
            old_concepts = [r.preferred_label for r in cookie.folio_concepts]
            new_concepts = [r.preferred_label for r in topic.folio_concepts]
            changed = (
                old_areas != new_areas
                or old_entities != new_entities
                or old_concepts != new_concepts
            )
            if changed:
                report.changed += 1
                report.changes.append({
                    "id": cookie.id,
                    "headline": cookie.headline[:80],
                    "old_areas": old_areas,
                    "new_areas": new_areas,
                    "old_entities_count": len(old_entities),
                    "new_entities_count": len(new_entities),
                    "old_concepts_count": len(old_concepts),
                    "new_concepts_count": len(new_concepts),
                })
            if progress is not None:
                status = "CHANGED" if changed else "SAME"
                progress(i, len(cookies), cookie, status,
                         f"areas: {old_areas} -> {new_areas}")
        else:
            changed = _update_cookie_tags(conn, cookie.id, topic)
            if changed:
                report.changed += 1
            if progress is not None:
                progress(i, len(cookies), cookie,
                         "CHANGED" if changed else "SAME", "")

    return report