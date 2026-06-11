"""Tests for FOLIO resolution (folio.py) and Singapore mappings."""

import httpx
import pytest
import respx

from sg_law_cookies import folio
from sg_law_cookies.folio import (
    FOLIO_API_BASE,
    SearchResult,
    pick_best_match,
    resolve_topic,
)
from sg_law_cookies.models import TopicExtraction
from sg_law_cookies.sg_mappings import lookup_sg_entity


@pytest.fixture(autouse=True)
def _clear_cache():
    folio.clear_cache()
    yield
    folio.clear_cache()


def _topic(**kwargs) -> TopicExtraction:
    return TopicExtraction(
        headline="h", summary="s", why_it_matters="w", significance="low", **kwargs
    )


def _owl_class(label: str, iri_id: str, children: list[str] | None = None) -> dict:
    return {
        "iri": f"{FOLIO_API_BASE}/{iri_id}",
        "label": label,
        "parent_class_of": children or [],
        "sub_class_of": [],
        "definition": None,
        "alternative_labels": [],
        "deprecated": False,
    }


def _path_payload(root_label: str) -> dict:
    return {"path": [{"iri": f"{FOLIO_API_BASE}/Rroot", "label": root_label, "id": "Rroot"}]}


# ── pick_best_match ──────────────────────────────────────────────


def test_pick_best_match_exact_case_insensitive():
    results = [SearchResult(iri="i1", label="Employment Law", relevance=0.5)]
    best = pick_best_match(results, "employment law")
    assert best is not None
    assert best.score == 1.0
    assert best.iri == "i1"


def test_pick_best_match_substring_either_direction():
    results = [SearchResult(iri="i1", label="Constructive Dismissal", relevance=0.0)]
    assert pick_best_match(results, "dismissal").score == 0.8
    assert pick_best_match(results, "constructive dismissal claims and remedies").score == 0.8


def test_pick_best_match_uses_normalised_relevance():
    results = [SearchResult(iri="i1", label="Employment Arbitration Rules", relevance=0.9)]
    best = pick_best_match(results, "employment dispute")
    assert best.score == 0.9


def test_pick_best_match_below_threshold_returns_none():
    results = [SearchResult(iri="i1", label="Saint Barthélemy", relevance=0.0)]
    assert pick_best_match(results, "estoppel") is None
    assert pick_best_match([], "estoppel") is None


def test_pick_best_match_threshold_overridable():
    results = [SearchResult(iri="i1", label="Corporate Governance", relevance=0.5)]
    assert pick_best_match(results, "widget", threshold=0.6) is None
    best = pick_best_match(results, "widget", threshold=0.4)  # falls back to relevance
    assert best is not None
    assert best.score == 0.5


def test_pick_best_match_prefers_leaf_on_tie():
    parent = SearchResult(iri="parent", label="Law of Torts", relevance=0.0, is_leaf=False)
    leaf = SearchResult(iri="leaf", label="Torts and Negligence", relevance=0.0, is_leaf=True)
    best = pick_best_match([parent, leaf], "torts")  # both substring -> 0.8
    assert best.iri == "leaf"


# ── resolve_topic, offline (respx) ───────────────────────────────


@respx.mock
def test_resolve_area_exact_match():
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(
        json={"classes": [_owl_class("Employment Law", "R7H5")], "properties": []}
    )
    topic = _topic(raw_areas=["employment law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert len(topic.folio_areas) == 1
    ref = topic.folio_areas[0]
    assert ref.iri == f"{FOLIO_API_BASE}/R7H5"
    assert ref.preferred_label == "Employment Law"
    assert ref.branch == "areas_of_law"
    assert ref.confidence == 1.0
    assert topic.unresolved == []


@respx.mock
def test_resolve_concept_substring_match():
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [[_owl_class("Constructive Dismissal", "Rcd1"), 90.0]]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Objectives")
    )
    topic = _topic(raw_concepts=["dismissal"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert len(topic.folio_concepts) == 1
    ref = topic.folio_concepts[0]
    assert ref.preferred_label == "Constructive Dismissal"
    assert ref.confidence == 0.8
    assert ref.branch == "objectives"
    assert topic.unresolved == []


@respx.mock
def test_resolve_concept_below_threshold_goes_unresolved():
    # API junk floor: unrelated labels still score ~90/100.
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={
            "results": [
                [_owl_class("Saint Barthélemy", "Rj1"), 90.0],
                [_owl_class("Wisconsin State Courts", "Rj2"), 90.0],
            ]
        }
    )
    topic = _topic(raw_concepts=["promissory estoppel"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert topic.folio_concepts == []
    assert topic.unresolved == ["promissory estoppel"]


@respx.mock
def test_resolve_entity_unresolvable_gets_placeholder_ref():
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(json={"results": []})
    topic = _topic(raw_entities=["Widget Licensing Tribunal"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert len(topic.folio_entities) == 1
    ref = topic.folio_entities[0]
    assert ref.iri is None
    assert ref.preferred_label == "Widget Licensing Tribunal"
    assert ref.branch == "unresolved"
    assert ref.confidence == 0.0
    assert topic.unresolved == ["Widget Licensing Tribunal"]


@respx.mock
def test_sg_mapping_short_circuits_api():
    route = respx.get(url__regex=rf"{FOLIO_API_BASE}/.*").respond(json={"results": []})
    topic = _topic(raw_entities=["PDPC", "Monetary Authority of Singapore"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert route.call_count == 0
    assert [r.preferred_label for r in topic.folio_entities] == [
        "Personal Data Protection Commission",
        "Monetary Authority of Singapore",
    ]
    assert all(r.branch == "sg_local" for r in topic.folio_entities)
    assert all(r.confidence == 1.0 for r in topic.folio_entities)
    assert topic.unresolved == []


def test_lookup_sg_entity_normalises_and_copies():
    a = lookup_sg_entity("  cpf  board ")
    b = lookup_sg_entity("CPF Board")
    assert a is not None and b is not None
    assert a.preferred_label == "Central Provident Fund Board"
    assert a is not b  # callers get copies, not shared state
    assert lookup_sg_entity("Unknown Body") is None


@respx.mock
def test_search_cache_avoids_repeat_api_calls():
    route = respx.get(f"{FOLIO_API_BASE}/search/query").respond(
        json={"classes": [_owl_class("Tax Law", "Rtax")], "properties": []}
    )
    topic_a = _topic(raw_areas=["Tax Law"])
    topic_b = _topic(raw_areas=["tax law"])  # cache key is case-insensitive
    with httpx.Client() as client:
        resolve_topic(topic_a, client)
        resolve_topic(topic_b, client)
    assert route.call_count == 1
    assert topic_b.folio_areas[0].confidence == 1.0


# ── live ─────────────────────────────────────────────────────────


@pytest.mark.live
def test_live_resolve_employment_law():
    topic = _topic(raw_areas=["Employment Law"])
    with httpx.Client(timeout=30) as client:
        resolve_topic(topic, client)
    assert len(topic.folio_areas) == 1
    ref = topic.folio_areas[0]
    assert ref.preferred_label == "Employment Law"
    assert ref.confidence == 1.0
    assert ref.iri and "openlegalstandard.org" in ref.iri
    assert topic.unresolved == []


# ── API failures degrade to unresolved, never crash the run ──────


@respx.mock
def test_folio_500_lands_in_unresolved_not_raise():
    folio._search_cache.clear()
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(500)
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(500)
    topic = TopicExtraction(
        headline="h", summary="s", why_it_matters="w", significance="low",
        raw_areas=["Employment"], raw_entities=["X"], raw_concepts=["duty of care"],
    )
    resolved = folio.resolve_topic(topic, httpx.Client())
    assert "Employment" in resolved.unresolved
    assert "duty of care" in resolved.unresolved
    assert resolved.folio_areas == []


@respx.mock
def test_single_char_query_skips_api():
    folio._search_cache.clear()
    route = respx.get(f"{FOLIO_API_BASE}/search/label").respond(200, json={"results": []})
    topic = TopicExtraction(
        headline="h", summary="s", why_it_matters="w", significance="low",
        raw_areas=[], raw_entities=["X"], raw_concepts=[],
    )
    folio.resolve_topic(topic, httpx.Client())
    assert not route.called
