"""FOLIO ontology resolution (PRD section 4.3, pseudocode section 4).

Resolution is powered by folio-resolve (github.com/damienriehl/folio-resolve),
an MIT-licensed matching engine. A custom ``RestApiOntologyProvider`` wraps
the FOLIO REST API at folio.openlegalstandard.org, and a ``MatchPipeline``
applies word-order-invariant scoring, place-name/short-label gates, alias
blocklist, and span decomposition on top of the API results.

API endpoints used:
- GET /search/label?query=<q>  — fuzzy search across all branches, returns
  {"results": [[OWLClass, score], ...]} with scores on a 0-100 scale.
- GET /search/query?label=<q>&branch=<branch>  — branch-filtered substring
  search, returns {"classes": [...]} with no relevance scores.
- GET /taxonomy/tree/path/<id>  — path from root, used to derive the branch
  of a matched concept.

The pipeline and provider are module-level singletons, built on first use
and cached for the process lifetime. ``clear_cache()`` resets them (for tests).

Areas of law are resolved from the closed vocabulary (``area_vocab``) —
no API call. Singapore-specific entities are resolved from ``sg_mappings.py``
before the pipeline — no API call.
"""

from __future__ import annotations

import httpx

from sg_law_cookies.folio_resolve_adapter import (
    AREAS_OF_LAW_BRANCH,
    CONFIDENCE_THRESHOLD,
    FOLIO_API_BASE,
    FORUMS_VENUES_BRANCH,
    LEGAL_AUTHORITIES_BRANCH,
    _UNRESOLVED_BRANCH,
    clear_cache as _adapter_clear_cache,
    resolve_judgment_meta as _adapter_resolve_judgment_meta,
    resolve_legislation as _adapter_resolve_legislation,
    resolve_topic as _adapter_resolve_topic,
    resolve_venue as _adapter_resolve_venue,
)
from sg_law_cookies.models import FolioRef, JudgmentMeta, TopicExtraction


def clear_cache() -> None:
    """Clear the cached pipeline and provider (for tests)."""
    _adapter_clear_cache()


def _unresolved_ref(label: str) -> FolioRef:
    return FolioRef(iri=None, preferred_label=label, branch=_UNRESOLVED_BRANCH, confidence=0.0)


def resolve_topic(topic: TopicExtraction, client: httpx.Client) -> TopicExtraction:
    """Resolve a topic's free-text labels to FOLIO IRIs (three passes).

    1. Areas: closed-vocabulary lookup (no network).
    2. Entities: local SG mappings first, then folio-resolve pipeline.
    3. Concepts: folio-resolve pipeline across all branches.
    """
    return _adapter_resolve_topic(topic, client)


def resolve_venue(client: httpx.Client, court_name: str) -> FolioRef:
    """Resolve a court/forum name (PRD 4.2 step 5: FOLIO forums/venues branch).

    The local Singapore table is checked FIRST. Non-Singapore courts
    resolve via branch-filtered FOLIO search + folio-resolve scoring.
    Always returns a FolioRef; degrades to unresolved on no match.
    """
    return _adapter_resolve_venue(client, court_name)


def resolve_legislation(client: httpx.Client, name: str) -> FolioRef:
    """Resolve a legislation name against FOLIO legal_authorities.

    Singapore statutes are mostly absent from FOLIO (expected — PRD 4.3).
    Falls back to local table, then unresolved placeholder. Never raises.
    """
    return _adapter_resolve_legislation(client, name)


def resolve_judgment_meta(client: httpx.Client, meta: JudgmentMeta) -> JudgmentMeta:
    """Resolve a JudgmentMeta in place: court, issue concepts, legislation.

    Interface contract: raw free-text labels arrive as placeholder refs
    (``FolioRef(iri=None, preferred_label=<raw label>, branch="unresolved",
    confidence=0.0)``). Placeholders are re-resolved; refs with an IRI
    are left untouched. Idempotent and safe to re-run.

    Never raises on API failure: degrades to unresolved placeholders.
    """
    return _adapter_resolve_judgment_meta(client, meta)