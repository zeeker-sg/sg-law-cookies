"""Adapter: route FOLIO resolution through folio-resolve (Phase 0 spike).

When the ``FOLIO_RESOLVE`` env var is truthy, ``resolve_topic`` and
``resolve_judgment_meta`` delegate here instead of using the legacy REST
API search/match layer in ``folio.py``.

Design:
- Areas of law: still resolved from the closed vocabulary (``area_vocab``),
  same as the legacy path — no pipeline needed.
- Singapore entities: still resolved from ``sg_mappings.py`` first, same
  as the legacy path.
- Everything else (concepts, entities, venues, legislation): resolved
  through a folio-resolve ``MatchPipeline`` backed by an
  ``InMemoryOntology`` populated from the FOLIO REST API at startup.

The ontology is fetched once (all branches, all concepts) and cached
for the process lifetime. This replaces per-query REST calls with
in-memory matching — the core architectural shift folio-resolve brings.

Scores from folio-resolve are 0-100; our FolioRef.confidence is 0.0-1.0.
We normalise by dividing by 100.
"""

from __future__ import annotations

import logging
import httpx
from folio_resolve import (
    Concept,
    InMemoryOntology,
    MatchCandidate,
    MatchPipeline,
    PlaceNameGate,
)

from sg_law_cookies.area_vocab import AREA_IRI_BY_LABEL
from sg_law_cookies.folio import (
    AREAS_OF_LAW_BRANCH,
    FOLIO_API_BASE,
    FORUMS_VENUES_BRANCH,
    LEGAL_AUTHORITIES_BRANCH,
    CONFIDENCE_THRESHOLD,
    _unresolved_ref,
)
from sg_law_cookies.models import FolioRef, JudgmentMeta, TopicExtraction
from sg_law_cookies.sg_mappings import lookup_sg_entity

logger = logging.getLogger(__name__)

# folio-resolve scores are 0-100; our confidence is 0.0-1.0.
_SCORE_SCALE = 100.0

# folio-resolve's default score_floor is 45.0. We convert our 0.6
# confidence threshold to the 0-100 scale to filter candidates.
# But score_floor operates inside the pipeline (drops candidates below it
# before returning). We set it to match our threshold so the pipeline
# does the filtering.
_SCORE_FLOOR = CONFIDENCE_THRESHOLD * _SCORE_SCALE  # 60.0

# Singapore place names to add to the PlaceNameGate. Prevents
# geographic false positives on Singapore-related queries.
_SG_PLACE_TOKENS: frozenset[str] = frozenset({
    "singapore", "johor", "jurong", "tampines", "woodlands",
    "changi", "sentosa", "bedok", "bishan", "geylang",
    "hougang", "ang", "mo", "kio", "toa", "payoh",
    "serangoon", "punggol", "sengkang", "yishun", "katong",
})

# Branch filter: folio-resolve matches across all branches by default.
# Our legacy code routes venue queries to forums_venues and legislation
# to legal_authorities. We replicate this by filtering the ontology
# to the relevant branch subset for those queries.

# Cache the pipeline for the process lifetime.
_pipeline: MatchPipeline | None = None
_ontology: InMemoryOntology | None = None


def _fetch_all_concepts(client: httpx.Client) -> list[Concept]:
    """Fetch all concepts from the FOLIO REST API across all branches.

    Uses /search/label with a broad query to populate the in-memory
    ontology. The FOLIO API does not have a "list all" endpoint, so we
    harvest via branch-filtered /search/query calls for each known
    branch, plus a broad /search/label sweep.

    For the spike, we use /search/label with single-letter queries to
    sweep the ontology. This is a rough approach — Phase 1 will refine
    this (likely via folio-python or a cached snapshot).
    """
    concepts: list[Concept] = []
    seen_iris: set[str] = set()

    # Known FOLIO branches (from PRD and folio.py).
    branches = [
        "areas_of_law",
        "forums_venues",
        "legal_authorities",
        "objectives",
        "elements",
        "services",
        "legal_information",
        "persons",
    ]

    for branch in branches:
        try:
            resp = client.get(
                f"{FOLIO_API_BASE}/search/query",
                params={"label": "*", "branch": branch, "limit": 500},
            )
            resp.raise_for_status()
            classes = resp.json().get("classes", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("FOLIO branch %s fetch failed: %s", branch, exc)
            continue
        for cls in classes:
            iri = cls.get("iri")
            label = cls.get("label")
            if not iri or not label or iri in seen_iris:
                continue
            seen_iris.add(iri)
            concepts.append(
                Concept(
                    iri=iri,
                    label=label,
                    branch=branch,
                    alternative_labels=tuple(cls.get("alternative_labels", [])),
                )
            )

    logger.info("Fetched %d concepts from FOLIO API", len(concepts))
    return concepts


def _build_pipeline(client: httpx.Client) -> MatchPipeline:
    """Build and cache a MatchPipeline from the FOLIO REST API."""
    global _pipeline, _ontology
    if _pipeline is not None:
        return _pipeline

    concepts = _fetch_all_concepts(client)
    if not concepts:
        # Fallback: at least load the area vocab concepts so the
        # pipeline isn't empty. This means concepts/entities won't
        # resolve, but areas still work (they bypass the pipeline).
        logger.warning(
            "No concepts fetched from FOLIO API; "
            "falling back to area-vocab-only ontology"
        )
        concepts = [
            Concept(iri=iri, label=label, branch=AREAS_OF_LAW_BRANCH)
            for label, iri in AREA_IRI_BY_LABEL.items()
        ]

    # Also include area vocab concepts (they may not appear in the
    # /search/query results due to API pagination/limits).
    area_iris = {iri for iri in AREA_IRI_BY_LABEL.values()}
    existing_iris = {c.iri for c in concepts}
    for label, iri in AREA_IRI_BY_LABEL.items():
        if iri not in existing_iris:
            concepts.append(
                Concept(iri=iri, label=label, branch=AREAS_OF_LAW_BRANCH)
            )

    _ontology = InMemoryOntology(concepts)
    _pipeline = MatchPipeline(
        ontology=_ontology,
        place_gate=PlaceNameGate(
            extra_tokens=_SG_PLACE_TOKENS,
            extra_markers=("republic of", "city of"),
        ),
        score_floor=_SCORE_FLOOR,
    )
    return _pipeline


def _candidate_to_folio_ref(
    candidate: MatchCandidate, fallback_branch: str = ""
) -> FolioRef:
    """Convert a folio-resolve MatchCandidate to our FolioRef."""
    return FolioRef(
        iri=candidate.iri,
        preferred_label=candidate.label,
        branch=candidate.branch or fallback_branch,
        confidence=candidate.score / _SCORE_SCALE,
    )


def _resolve_via_pipeline(
    client: httpx.Client, term: str, branch: str | None = None
) -> FolioRef | None:
    """Resolve a free-text label through the folio-resolve pipeline.

    Returns a FolioRef if a match is found above threshold, else None.
    """
    pipe = _build_pipeline(client)
    candidates = pipe.match(term)

    # Filter by branch if specified (venue/legislation routing).
    if branch and candidates:
        candidates = [c for c in candidates if c.branch == branch]

    if not candidates:
        return None

    best = candidates[0]  # pipeline returns sorted by score descending
    return _candidate_to_folio_ref(best)


# ── Public API (mirrors folio.py's interface) ──────────────────────


def resolve_topic(
    topic: TopicExtraction, client: httpx.Client
) -> TopicExtraction:
    """Resolve a topic's free-text labels to FOLIO IRIs via folio-resolve.

    Same three-pass structure as folio.resolve_topic:
    1. Areas: closed-vocabulary lookup (no pipeline, no network).
    2. Entities: local SG mappings first, then pipeline.
    3. Concepts: pipeline across all branches.
    """
    # Pass 1: areas of law — closed vocabulary, exact-label lookup.
    seen_areas: set[str] = set()
    for raw_area in topic.raw_areas:
        if raw_area in seen_areas:
            continue
        seen_areas.add(raw_area)
        iri = AREA_IRI_BY_LABEL.get(raw_area)
        if iri is not None:
            topic.folio_areas.append(
                FolioRef(
                    iri=iri,
                    preferred_label=raw_area,
                    branch=AREAS_OF_LAW_BRANCH,
                    confidence=1.0,
                )
            )
        else:
            topic.unresolved.append(raw_area)

    # Pass 2: entities — local Singapore mappings first, then pipeline.
    for raw_entity in topic.raw_entities:
        local = lookup_sg_entity(raw_entity)
        if local:
            topic.folio_entities.append(local)
            continue
        ref = _resolve_via_pipeline(client, raw_entity)
        if ref:
            topic.folio_entities.append(ref)
        else:
            topic.folio_entities.append(
                FolioRef(
                    iri=None,
                    preferred_label=raw_entity,
                    branch="unresolved",
                    confidence=0.0,
                )
            )
            topic.unresolved.append(raw_entity)

    # Pass 3: legal concepts across all branches.
    for raw_concept in topic.raw_concepts:
        ref = _resolve_via_pipeline(client, raw_concept)
        if ref:
            topic.folio_concepts.append(ref)
        else:
            topic.unresolved.append(raw_concept)

    return topic


def resolve_venue(client: httpx.Client, court_name: str) -> FolioRef:
    """Resolve a court/forum name via folio-resolve.

    Local Singapore table checked first (same as legacy path).
    """
    local = lookup_sg_entity(court_name)
    if local:
        return local
    ref = _resolve_via_pipeline(client, court_name, branch=FORUMS_VENUES_BRANCH)
    if ref:
        return ref
    return _unresolved_ref(court_name)


def resolve_legislation(client: httpx.Client, name: str) -> FolioRef:
    """Resolve legislation via folio-resolve.

    Local Singapore table checked first (same as legacy path).
    """
    local = lookup_sg_entity(name)
    if local:
        return local
    ref = _resolve_via_pipeline(client, name, branch=LEGAL_AUTHORITIES_BRANCH)
    if ref:
        return ref
    return _unresolved_ref(name)


def resolve_judgment_meta(
    client: httpx.Client, meta: JudgmentMeta
) -> JudgmentMeta:
    """Resolve a JudgmentMeta in place: court, issue concepts, legislation.

    Same interface contract as folio.resolve_judgment_meta — placeholder
    refs (iri=None) are re-resolved; refs with an IRI are left untouched.
    Idempotent and safe to re-run.
    """
    if meta.court is not None and meta.court.iri is None:
        meta.court = resolve_venue(client, meta.court.preferred_label)

    for issue in meta.issues:
        resolved: list[FolioRef] = []
        for ref in issue.folio_concepts:
            if ref.iri is not None:
                resolved.append(ref)
                continue
            hit = _resolve_via_pipeline(client, ref.preferred_label)
            resolved.append(hit if hit else _unresolved_ref(ref.preferred_label))
        issue.folio_concepts = resolved

    meta.legislation = [
        ref if ref.iri is not None
        else resolve_legislation(client, ref.preferred_label)
        for ref in meta.legislation
    ]
    return meta


def clear_cache() -> None:
    """Clear the cached pipeline (mainly for tests)."""
    global _pipeline, _ontology
    _pipeline = None
    _ontology = None


def set_pipeline(pipeline: MatchPipeline | None) -> None:
    """Inject a pre-built pipeline (for testing)."""
    global _pipeline, _ontology
    _pipeline = pipeline
    _ontology = None if pipeline is None else getattr(pipeline, "_ontology", None)