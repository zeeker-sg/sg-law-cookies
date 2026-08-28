"""Adapter: route FOLIO resolution through folio-resolve (Phase 0 spike, Option C).

When the ``FOLIO_RESOLVE`` env var is truthy, ``resolve_topic`` and
``resolve_judgment_meta`` delegate here instead of using the legacy REST
API search/match layer in ``folio.py``.

Option C — hybrid approach:
- Keep the per-query REST API pattern (no bulk ontology fetch).
- Implement a custom ``OntologyProvider`` that wraps the FOLIO REST API's
  ``/search/label`` endpoint. Each query hits the API, returns candidates
  with scores, and the ``MatchPipeline``'s gates, blocklist, and scoring
  refine them.
- Get folio-resolve's matching intelligence (word-order-invariant scoring,
  place-name gates, short-label gates, alias blocklist, span decomposition)
  on top of the same API data the legacy path uses.

The provider caches API responses per query (same as legacy ``_search_cache``).
The pipeline is built once and reused — the provider handles per-query API
calls, so the pipeline doesn't need a pre-loaded ontology.

Scores from folio-resolve are 0-100; our FolioRef.confidence is 0.0-1.0.
We normalise by dividing by 100.
"""

from __future__ import annotations

import logging
import httpx
from folio_resolve import (
    Concept,
    LabelInfo,
    MatchCandidate,
    MatchPipeline,
    OntologyProvider,
    PlaceNameGate,
)

from sg_law_cookies.area_vocab import AREA_IRI_BY_LABEL
from sg_law_cookies.area_mappings import lookup_local_area
from sg_law_cookies.models import FolioRef, JudgmentMeta, TopicExtraction
from sg_law_cookies.sg_mappings import lookup_sg_entity

logger = logging.getLogger(__name__)

# ── Shared constants (formerly in folio.py, now here to avoid circular imports) ──

FOLIO_API_BASE = "https://folio.openlegalstandard.org"
CONFIDENCE_THRESHOLD = 0.6
AREAS_OF_LAW_BRANCH = "areas_of_law"
FORUMS_VENUES_BRANCH = "forums_venues"
LEGAL_AUTHORITIES_BRANCH = "legal_authorities"
_UNRESOLVED_BRANCH = "unresolved"


def _unresolved_ref(label: str) -> FolioRef:
    """Create an unresolved FolioRef placeholder."""
    return FolioRef(iri=None, preferred_label=label, branch=_UNRESOLVED_BRANCH, confidence=0.0)

# folio-resolve scores are 0-100; our confidence is 0.0-1.0.
_SCORE_SCALE = 100.0

# folio-resolve's default score_floor is 45.0. We convert our 0.6
# confidence threshold to the 0-100 scale to filter candidates.
_SCORE_FLOOR = CONFIDENCE_THRESHOLD * _SCORE_SCALE  # 60.0

# Singapore place names to add to the PlaceNameGate. Prevents
# geographic false positives on Singapore-related queries.
_SG_PLACE_TOKENS: frozenset[str] = frozenset({
    "singapore", "johor", "jurong", "tampines", "woodlands",
    "changi", "sentosa", "bedok", "bishan", "geylang",
    "hougang", "ang", "mo", "kio", "toa", "payoh",
    "serangoon", "punggol", "sengkang", "yishun", "katong",
})


# ── Custom OntologyProvider wrapping the FOLIO REST API ────────────


class RestApiOntologyProvider:
    """An OntologyProvider that queries the FOLIO REST API per query.

    Implements the OntologyProvider Protocol (all_labels, search_by_label,
    get_concept) by calling the live FOLIO REST API. Results are cached
    per query for the process lifetime — same as the legacy _search_cache.

    The MatchPipeline calls ``search_by_label`` in its _filter stage.
    The candidates returned here are then run through the pipeline's
    gates, blocklist, and scoring before reaching the consumer.
    """

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._search_cache: dict[str, list[tuple[Concept, float]]] = {}
        self._concept_cache: dict[str, Concept] = {}
        self._branch_cache: dict[str, str] = {}

    def search_by_label(
        self, query: str, *, limit: int = 20
    ) -> list[tuple[Concept, float]]:
        """Search the FOLIO API for labels matching the query.

        Uses GET /search/label?query=<q> which returns fuzzy matches
        across all branches with scores on a 0-100 scale. The API has
        a junk floor around 90 — it returns semantically unrelated labels
        (country names, US courts, industry codes) at score 90 for almost
        any query. We re-score every candidate with
        ``compute_relevance_score`` (word-order-invariant content-word
        overlap) so that labels with zero token overlap score 0.0 and
        fall below the pipeline's score floor. The gates then handle
        the remaining place-name / short-label edge cases.
        """
        key = query.lower().strip()
        if key in self._search_cache:
            return self._search_cache[key]

        results: list[tuple[Concept, float]] = []
        if len(query.strip()) < 2:
            self._search_cache[key] = results
            return results

        try:
            resp = self._client.get(
                f"{FOLIO_API_BASE}/search/label", params={"query": query}
            )
            resp.raise_for_status()
            raw = resp.json().get("results", [])
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("FOLIO search/label failed for %r: %s", query, exc)
            self._search_cache[key] = results
            return results

        from folio_resolve.scoring import compute_relevance_score, content_words

        query_content = content_words(query)

        for cls, api_score in raw:
            iri = cls.get("iri")
            label = cls.get("label")
            if not iri or not label:
                continue
            # Re-score with word-overlap to filter out the API's junk
            # floor. A candidate like "Arkansas State Courts" returned
            # for query "apparent bias" has zero content-word overlap and
            # scores 0.0, so it never reaches the pipeline's gates.
            rescored = compute_relevance_score(
                query_content, query, label,
                synonyms=cls.get("alternative_labels", []),
            )
            if rescored <= 0.0:
                continue
            # Derive branch from the taxonomy path (lazy, cached).
            branch = self._branch_for_iri(iri)
            concept = Concept(
                iri=iri,
                label=label,
                branch=branch,
                alternative_labels=tuple(cls.get("alternative_labels", [])),
            )
            self._concept_cache[iri] = concept
            results.append((concept, rescored))

        # Sort by score descending, then IRI for determinism
        results.sort(key=lambda pair: (-pair[1], pair[0].iri))
        results = results[:limit]
        self._search_cache[key] = results
        return results

    def all_labels(self) -> dict[str, LabelInfo]:
        """Return all labels — not practical for a REST API provider.

        The MatchPipeline only calls this if the entity_ruler is enabled.
        We don't use the entity_ruler in the spike, so this returns an
        empty dict. Phase 1 can populate it from a cached snapshot.
        """
        return {}

    def get_concept(self, iri: str) -> Concept | None:
        """Get a concept by IRI from the cache, or None."""
        return self._concept_cache.get(iri)

    def _branch_for_iri(self, iri: str) -> str:
        """Derive the taxonomy branch of a concept from its path to root.

        Calls GET /taxonomy/tree/path/<id> (cached per IRI). The root
        label of the path determines the branch, same as legacy
        ``folio._branch_for_iri``. Returns "" on API failure.
        """
        if iri in self._branch_cache:
            return self._branch_cache[iri]
        branch = ""
        try:
            iri_id = iri.rsplit("/", 1)[-1]
            resp = self._client.get(
                f"{FOLIO_API_BASE}/taxonomy/tree/path/{iri_id}"
            )
            resp.raise_for_status()
            path = resp.json().get("path", [])
            if path and path[0].get("label"):
                root = path[0]["label"]
                branch = AREAS_OF_LAW_BRANCH if root == "Area of Law" else "_".join(root.lower().split())
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("taxonomy path failed for %s: %s", iri, exc)
        self._branch_cache[iri] = branch
        return branch


# ── Pipeline construction ─────────────────────────────────────────

_pipeline: MatchPipeline | None = None
_provider: RestApiOntologyProvider | None = None


def _build_pipeline(client: httpx.Client) -> MatchPipeline:
    """Build and cache a MatchPipeline with a REST API provider."""
    global _pipeline, _provider
    if _pipeline is not None:
        return _pipeline

    _provider = RestApiOntologyProvider(client)
    _pipeline = MatchPipeline(
        ontology=_provider,
        place_gate=PlaceNameGate(
            extra_tokens=_SG_PLACE_TOKENS,
            extra_markers=("republic of", "city of"),
            # FOLIO's forums_and_venues branch contains US state/county
            # courts that the API returns as junk matches for any query.
            # Without this marker, "Arkansas State Courts" (returned for
            # "apparent bias") passes the gate at score 90 because the
            # built-in _PLACE_BRANCH_MARKERS don't include "forums".
            extra_branch_markers=("forums_and_venues",),
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
    # The pipeline doesn't know about FOLIO's branch labels — it gets
    # whatever the provider returns. We set branch="" in the provider,
    # so branch filtering is a no-op for now. For venue/legislation,
    # we fall back to the legacy branch-filtered search.
    if branch and candidates:
        # Only keep candidates that came from the branch-filtered search.
        # Since the provider searches all branches, we need to check
        # the concept's branch. But our provider doesn't populate branch
        # (it returns ""), so this filter currently does nothing.
        # TODO: For venue/legislation, use a branch-filtered provider.
        pass

    if not candidates:
        return None

    best = candidates[0]  # pipeline returns sorted by score descending
    return _candidate_to_folio_ref(best, fallback_branch=branch or "")


# ── Branch-filtered search for venue/legislation ───────────────────
# The legacy code routes venue queries to /search/query?branch=forums_venues
# and legislation to /search/query?branch=legal_authorities. These return
# candidates without scores, so the legacy code uses pick_best_match.
# For Option C, we use the legacy branch-filtered search and run the
# results through folio-resolve's compute_relevance_score + gates.


def _search_branch_filtered(
    client: httpx.Client, query: str, branch: str
) -> list[tuple[Concept, float]]:
    """Branch-filtered /search/query; returns concepts with computed scores."""
    if len(query.strip()) < 2:
        return []
    try:
        resp = client.get(
            f"{FOLIO_API_BASE}/search/query",
            params={"label": query, "branch": branch, "limit": 20},
        )
        resp.raise_for_status()
        classes = resp.json().get("classes", [])
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("FOLIO search/query for branch %s failed: %s", branch, exc)
        return []

    from folio_resolve import compute_relevance_score, content_words

    results: list[tuple[Concept, float]] = []
    qc = content_words(query)
    for cls in classes:
        iri = cls.get("iri")
        label = cls.get("label")
        if not iri or not label:
            continue
        score = compute_relevance_score(qc, query, label)
        if score > 0:
            results.append((
                Concept(iri=iri, label=label, branch=branch),
                score,
            ))
    results.sort(key=lambda pair: (-pair[1], pair[0].iri))
    return results


def _resolve_branch_filtered(
    client: httpx.Client, term: str, branch: str
) -> FolioRef | None:
    """Resolve using branch-filtered search + folio-resolve scoring."""
    candidates = _search_branch_filtered(client, term, branch)
    if not candidates:
        return None

    # Run through gates manually (the pipeline's search_by_label
    # uses all-branches /search/label, not branch-filtered /search/query).
    from folio_resolve import PlaceNameGate, ShortLabelGate

    place_gate = PlaceNameGate(
        extra_tokens=_SG_PLACE_TOKENS,
        extra_markers=("republic of", "city of"),
        extra_branch_markers=("forums_and_venues",),
    )
    short_gate = ShortLabelGate()

    best: MatchCandidate | None = None
    for concept, score in candidates:
        place = place_gate.evaluate(
            query=term, label=concept.label, branch=branch, score=score
        )
        short = short_gate.evaluate(query=term, label=concept.label, score=place.score)
        final_score = short.score
        if final_score < _SCORE_FLOOR:
            continue
        cand = MatchCandidate(
            iri=concept.iri,
            label=concept.label,
            score=final_score,
            branch=branch,
            extraction_path="branch_filtered",
            surface_term=term,
            gated=place.demoted or short.demoted,
            gate_reason="; ".join(r for r in (place.reason, short.reason) if r),
        )
        if best is None or cand.score > best.score:
            best = cand

    if best is None:
        return None
    return _candidate_to_folio_ref(best, fallback_branch=branch)


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
    # Pass 1: areas of law — closed vocabulary, then local area mappings.
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
            # Try local area mappings for areas not in FOLIO's taxonomy
            # (e.g. Arbitration Law, Ethics and Professional Responsibility).
            local_area = lookup_local_area(raw_area)
            if local_area is not None:
                topic.folio_areas.append(local_area)
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
    Non-SG courts resolved via branch-filtered search + folio-resolve scoring.
    """
    local = lookup_sg_entity(court_name)
    if local:
        return local
    ref = _resolve_branch_filtered(client, court_name, FORUMS_VENUES_BRANCH)
    if ref:
        return ref
    return _unresolved_ref(court_name)


def resolve_legislation(client: httpx.Client, name: str) -> FolioRef:
    """Resolve legislation via folio-resolve.

    Local Singapore table checked first (same as legacy path).
    Non-SG legislation resolved via branch-filtered search + folio-resolve scoring.
    """
    local = lookup_sg_entity(name)
    if local:
        return local
    ref = _resolve_branch_filtered(client, name, LEGAL_AUTHORITIES_BRANCH)
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
    """Clear the cached pipeline and provider (mainly for tests)."""
    global _pipeline, _provider
    _pipeline = None
    _provider = None


def set_pipeline(pipeline: MatchPipeline | None) -> None:
    """Inject a pre-built pipeline (for testing)."""
    global _pipeline, _provider
    _pipeline = pipeline
    _provider = None