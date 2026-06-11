"""FOLIO ontology resolution (PRD section 4.3, pseudocode section 4).

Uses the hosted FOLIO REST API at folio.openlegalstandard.org:

- GET /search/query?label=<q>&branch=area_of_law  — branch-filtered substring
  search, returns {"classes": [...]} with no relevance scores.
- GET /search/label?query=<q>  — fuzzy search across all branches, returns
  {"results": [[OWLClass, score], ...]} with scores on a 0-100 scale. The
  scorer has a junk floor around 90, so raw scores are only trusted when the
  candidate label shares a token with the query.
- GET /taxonomy/tree/path/<id>  — path from root, used to derive the branch
  of a matched concept.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel

from sg_law_cookies.models import FolioRef, TopicExtraction
from sg_law_cookies.sg_mappings import lookup_sg_entity

FOLIO_API_BASE = "https://folio.openlegalstandard.org"
CONFIDENCE_THRESHOLD = 0.6
AREAS_OF_LAW_BRANCH = "areas_of_law"  # branch label stored on FolioRef
_AREAS_QUERY_BRANCH = "area_of_law"  # branch filter value for /search/query
_SEARCH_LIMIT = 20


class SearchResult(BaseModel):
    iri: str
    label: str
    relevance: float = 0.0  # normalised 0-1 API relevance
    is_leaf: bool | None = None  # True when the concept has no children
    score: float = 0.0  # final match score, set by pick_best_match


_search_cache: dict[tuple[str, str | None], list[SearchResult]] = {}
_branch_cache: dict[str, str] = {}


def clear_cache() -> None:
    _search_cache.clear()
    _branch_cache.clear()


def _tokens(text: str) -> set[str]:
    return {t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if len(t) >= 3}


def _normalised_relevance(api_score: float, query: str, label: str) -> float:
    # The API's fuzzy scorer gives unrelated labels ~90/100, so only trust
    # the score when query and label share at least one real token.
    if not _tokens(query) & _tokens(label):
        return 0.0
    return min(max(api_score / 100.0, 0.0), 1.0)


def _is_leaf(cls: dict) -> bool | None:
    children = cls.get("parent_class_of")
    if children is None:
        return None
    return len(children) == 0


def _search_areas(client: httpx.Client, query: str) -> list[SearchResult]:
    key = (query.lower(), _AREAS_QUERY_BRANCH)
    if key in _search_cache:
        return _search_cache[key]
    if len(query.strip()) < 2:
        _search_cache[key] = []
        return []
    try:
        resp = client.get(
            f"{FOLIO_API_BASE}/search/query",
            params={"label": query, "branch": _AREAS_QUERY_BRANCH, "limit": _SEARCH_LIMIT},
        )
        resp.raise_for_status()
        classes = resp.json().get("classes", [])
    except (httpx.HTTPError, ValueError):
        # An API failure must not kill the run — the term lands in
        # `unresolved` and can be re-resolved later (PRD 4.3, 8.2).
        return []
    results = [
        SearchResult(iri=cls["iri"], label=cls["label"], is_leaf=_is_leaf(cls))
        for cls in classes
        if cls.get("iri") and cls.get("label")
    ]
    _search_cache[key] = results
    return results


def _search_all_branches(client: httpx.Client, query: str) -> list[SearchResult]:
    key = (query.lower(), None)
    if key in _search_cache:
        return _search_cache[key]
    if len(query.strip()) < 2:
        _search_cache[key] = []
        return []
    try:
        resp = client.get(f"{FOLIO_API_BASE}/search/label", params={"query": query})
        resp.raise_for_status()
        raw = resp.json().get("results", [])
    except (httpx.HTTPError, ValueError):
        return []
    results = []
    for cls, api_score in raw:
        if not cls.get("iri") or not cls.get("label"):
            continue
        results.append(
            SearchResult(
                iri=cls["iri"],
                label=cls["label"],
                relevance=_normalised_relevance(float(api_score), query, cls["label"]),
                is_leaf=_is_leaf(cls),
            )
        )
    _search_cache[key] = results
    return results


def _branch_for_iri(client: httpx.Client, iri: str) -> str:
    """Derive the taxonomy branch of a concept from its path to the root."""
    if iri in _branch_cache:
        return _branch_cache[iri]
    branch = "unknown"
    try:
        resp = client.get(f"{FOLIO_API_BASE}/taxonomy/tree/path/{iri.rsplit('/', 1)[-1]}")
        resp.raise_for_status()
        path = resp.json().get("path", [])
        if path and path[0].get("label"):
            root = path[0]["label"]
            branch = AREAS_OF_LAW_BRANCH if root == "Area of Law" else "_".join(root.lower().split())
    except (httpx.HTTPError, ValueError):
        pass
    _branch_cache[iri] = branch
    return branch


def pick_best_match(
    results: list[SearchResult],
    query: str,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> SearchResult | None:
    """Rank results per PRD 4.3: exact 1.0, substring 0.8, else API relevance.

    Returns the best result with .score set, or None if all fall below
    `threshold`. On score ties, prefers leaf (more specific) concepts.
    """
    if not results:
        return None
    q = query.strip().lower()
    best: SearchResult | None = None
    best_key: tuple[float, int] = (-1.0, -1)
    for r in results:
        label = r.label.strip().lower()
        if label == q:
            score = 1.0
        elif q in label or label in q:
            score = 0.8
        else:
            score = r.relevance
        key = (score, 1 if r.is_leaf else 0)
        if key > best_key:
            best_key = key
            best = r
    assert best is not None
    if best_key[0] < threshold:
        return None
    return best.model_copy(update={"score": best_key[0]})


def resolve_topic(topic: TopicExtraction, client: httpx.Client) -> TopicExtraction:
    """Resolve a topic's free-text labels to FOLIO IRIs (three passes)."""

    # Pass 1: areas of law, constrained to the areas-of-law branch.
    for raw_area in topic.raw_areas:
        best = pick_best_match(_search_areas(client, raw_area), raw_area)
        if best:
            topic.folio_areas.append(
                FolioRef(
                    iri=best.iri,
                    preferred_label=best.label,
                    branch=AREAS_OF_LAW_BRANCH,
                    confidence=best.score,
                )
            )
        else:
            topic.unresolved.append(raw_area)

    # Pass 2: entities — local Singapore mappings first, then all branches.
    for raw_entity in topic.raw_entities:
        local = lookup_sg_entity(raw_entity)
        if local:
            topic.folio_entities.append(local)
            continue
        best = pick_best_match(_search_all_branches(client, raw_entity), raw_entity)
        if best:
            topic.folio_entities.append(
                FolioRef(
                    iri=best.iri,
                    preferred_label=best.label,
                    branch=_branch_for_iri(client, best.iri),
                    confidence=best.score,
                )
            )
        else:
            # Expected for Singapore-specific bodies absent from FOLIO:
            # keep the raw label and flag it for review.
            topic.folio_entities.append(
                FolioRef(iri=None, preferred_label=raw_entity, branch="unresolved", confidence=0.0)
            )
            topic.unresolved.append(raw_entity)

    # Pass 3: legal concepts (doctrines, tests, principles) across all branches.
    for raw_concept in topic.raw_concepts:
        best = pick_best_match(_search_all_branches(client, raw_concept), raw_concept)
        if best:
            topic.folio_concepts.append(
                FolioRef(
                    iri=best.iri,
                    preferred_label=best.label,
                    branch=_branch_for_iri(client, best.iri),
                    confidence=best.score,
                )
            )
        else:
            topic.unresolved.append(raw_concept)

    return topic
