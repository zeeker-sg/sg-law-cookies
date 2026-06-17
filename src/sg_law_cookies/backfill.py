"""Re-tag existing cookies' areas of law with the closed-vocabulary classifier.

Older cookies were tagged by the free-text + FOLIO-substring resolver, which
dropped ~a third of them to no area (rendered "General"). The model's original
area selection was never persisted, so we re-classify each cookie from its own
distilled text (headline / summary / why_it_matters) against the closed FOLIO
area vocabulary and overwrite folio_areas. Only folio_areas is touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sg_law_cookies import db
from sg_law_cookies.area_vocab import AREA_IRI_BY_LABEL, AREA_LABELS
from sg_law_cookies.llm import LLMBackend
from sg_law_cookies.models import Cookie, FolioRef
from sg_law_cookies.folio import AREAS_OF_LAW_BRANCH

CLASSIFY_AREAS_TOOL_NAME = "classify_areas"

CLASSIFY_AREAS_TOOL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "raw_areas": {
            "type": "array",
            "items": {"type": "string", "enum": AREA_LABELS},
            "description": (
                "Areas of law engaged by this legal development, chosen from the "
                "FOLIO area-of-law taxonomy. Pick the most specific applicable "
                "label(s); return an empty list if none genuinely fit."
            ),
        }
    },
    "required": ["raw_areas"],
    "additionalProperties": False,
}

CLASSIFY_AREAS_SYSTEM_PROMPT = """\
You classify a single Singapore legal development by area of law.

Choose the areas of law engaged, ONLY from the fixed list of labels offered in
the classify_areas tool's raw_areas field (the FOLIO area-of-law taxonomy). Pick
the most specific applicable label(s). The taxonomy is US-derived, so map the
Singapore concept to its nearest equivalent (e.g. autonomous-vehicle regulation
-> "Transportation Law", personal data protection -> "Privacy Law"). If no label
genuinely fits, return an empty list rather than forcing a poor match.
"""


@dataclass
class BackfillReport:
    total: int = 0
    considered: int = 0
    changed: int = 0
    now_tagged: int = 0  # were empty/General, now have an area
    now_empty: int = 0  # had an area, now empty
    failed: int = 0
    changes: list[tuple[str, list[str], list[str]]] = field(default_factory=list)


def _cookie_text(cookie: Cookie) -> str:
    return f"{cookie.headline}\n\n{cookie.summary}\n\n{cookie.why_it_matters}"


def classify_areas(backend: LLMBackend, cookie: Cookie) -> list[FolioRef]:
    """Run the closed-set area classifier for one cookie -> resolved FolioRefs."""
    data = backend.structured(
        system=CLASSIFY_AREAS_SYSTEM_PROMPT,
        user=_cookie_text(cookie),
        schema=CLASSIFY_AREAS_TOOL_SCHEMA,
        tool_name=CLASSIFY_AREAS_TOOL_NAME,
    )
    refs: list[FolioRef] = []
    seen: set[str] = set()
    for label in data.get("raw_areas") or []:
        iri = AREA_IRI_BY_LABEL.get(label)
        if iri is None or label in seen:
            continue
        seen.add(label)
        refs.append(
            FolioRef(
                iri=iri,
                preferred_label=label,
                branch=AREAS_OF_LAW_BRANCH,
                confidence=1.0,
            )
        )
    return refs


def _news_cookie_ids(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT cs.cookie_id FROM cookie_sources cs "
            "JOIN sources s ON cs.source_id = s.id WHERE s.item_type = 'news'"
        )
    }


def backfill_areas(
    conn,
    backend: LLMBackend,
    *,
    only_general: bool = False,
    news_only: bool = False,
    dry_run: bool = False,
    progress=None,
) -> BackfillReport:
    """Re-classify areas for stored cookies and overwrite folio_areas.

    only_general restricts to cookies that currently have no area; news_only to
    cookies with a news source (judgment areas are often genuine FOLIO gaps).
    progress, if given, is called as progress(index, total, cookie, old, new).
    """
    cookies = db.all_cookies(conn)
    news_ids = _news_cookie_ids(conn) if news_only else None
    report = BackfillReport(total=len(cookies))
    for i, cookie in enumerate(cookies):
        old_labels = [r.preferred_label for r in cookie.folio_areas]
        if only_general and old_labels:
            continue
        if news_ids is not None and cookie.id not in news_ids:
            continue
        report.considered += 1
        try:
            new_refs = classify_areas(backend, cookie)
        except Exception:  # noqa: BLE001 — one bad cookie must not abort the run
            report.failed += 1
            if progress is not None:
                progress(i, len(cookies), cookie, old_labels, None)
            continue
        new_labels = [r.preferred_label for r in new_refs]
        if progress is not None:
            progress(i, len(cookies), cookie, old_labels, new_labels)
        if new_labels == old_labels:
            continue
        report.changed += 1
        if old_labels and not new_labels:
            report.now_empty += 1
        elif not old_labels and new_labels:
            report.now_tagged += 1
        report.changes.append((cookie.id, old_labels, new_labels))
        if not dry_run:
            db.update_cookie_areas(conn, cookie.id, new_refs)
    return report
