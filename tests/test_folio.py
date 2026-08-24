"""Tests for FOLIO resolution (folio.py) and Singapore mappings.

The legacy hand-rolled matcher (``pick_best_match``, ``SearchResult``,
``_search_all_branches`` …) has been replaced by the folio-resolve
adapter (``folio_resolve_adapter.py``).  ``folio.py`` is now a thin
facade that delegates to the adapter, so these tests exercise the
adapter through the facade and mock the same FOLIO REST API endpoints
the adapter calls (``/search/label``, ``/search/query``,
``/taxonomy/tree/path``).
"""

import httpx
import pytest
import respx

from sg_law_cookies import folio
from sg_law_cookies.area_vocab import AREA_IRI_BY_LABEL
from sg_law_cookies.folio import (
    FOLIO_API_BASE,
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


# ── resolve_topic, offline (respx) ───────────────────────────────


@respx.mock
def test_resolve_area_from_vocab_no_api_call():
    # Areas come from the closed FOLIO vocabulary, so pass 1 is a dict lookup
    # and must not touch the network.
    route = respx.get(url__regex=rf"{FOLIO_API_BASE}/.*").respond(json={"classes": []})
    topic = _topic(raw_areas=["Employment Law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert route.call_count == 0
    assert len(topic.folio_areas) == 1
    ref = topic.folio_areas[0]
    assert ref.iri == AREA_IRI_BY_LABEL["Employment Law"]
    assert ref.preferred_label == "Employment Law"
    assert ref.branch == "areas_of_law"
    assert ref.confidence == 1.0
    assert topic.unresolved == []


def test_resolve_area_unknown_label_goes_unresolved():
    # A label outside the vocabulary (e.g. a backend ignoring the schema enum)
    # degrades to unresolved rather than being stored.
    topic = _topic(raw_areas=["employment law", "Made Up Law"])  # wrong case / not in set
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert topic.folio_areas == []
    assert topic.unresolved == ["employment law", "Made Up Law"]


def test_resolve_area_dedupes_repeated_labels():
    topic = _topic(raw_areas=["Tax Law", "Tax Law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert [r.preferred_label for r in topic.folio_areas] == ["Tax Law"]


@respx.mock
def test_resolve_concept_substring_match():
    # folio-resolve passes the API score through the gates unchanged when the
    # candidate is not a place-name / short-label, so an API score of 90.0
    # becomes confidence 0.9 (90/100) — not the legacy 0.8 substring score.
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
    assert ref.confidence == pytest.approx(0.9)
    assert ref.branch == "objectives"
    assert topic.unresolved == []


@respx.mock
def test_resolve_concept_below_threshold_goes_unresolved():
    # folio-resolve's gates demote place-name / short-label false positives
    # below the confidence floor (60/100), so they land in unresolved.
    # "Court of Samoa" contains a place-name token ("Samoa") which the
    # PlaceNameGate recognises and demotes to 40/100 — below the floor.
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [[_owl_class("Court of Samoa", "Rj1"), 90.0]]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Forums")
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
    route = respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [[_owl_class("Duty of Care", "Rdoc"), 100.0]]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Objectives")
    )
    topic_a = _topic(raw_concepts=["Duty of Care"])
    topic_b = _topic(raw_concepts=["duty of care"])  # cache key is case-insensitive
    with httpx.Client() as client:
        resolve_topic(topic_a, client)
        resolve_topic(topic_b, client)
    assert route.call_count == 1
    assert topic_b.folio_concepts[0].confidence == 1.0


# ── live ─────────────────────────────────────────────────────────


def test_resolve_area_iri_points_at_folio():
    topic = _topic(raw_areas=["Employment Law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    ref = topic.folio_areas[0]
    assert ref.iri and "openlegalstandard.org" in ref.iri


# ── API failures degrade to unresolved, never crash the run ──────


@respx.mock
def test_folio_500_lands_in_unresolved_not_raise():
    folio.clear_cache()
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
    folio.clear_cache()
    route = respx.get(f"{FOLIO_API_BASE}/search/label").respond(200, json={"results": []})
    topic = TopicExtraction(
        headline="h", summary="s", why_it_matters="w", significance="low",
        raw_areas=[], raw_entities=["X"], raw_concepts=[],
    )
    folio.resolve_topic(topic, httpx.Client())
    assert not route.called