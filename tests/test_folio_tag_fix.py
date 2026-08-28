"""Tests for the FOLIO tag garbage fix (3 bugs).

Bug 1: Areas not in FOLIO's vocabulary (Arbitration Law, Ethics and
Professional Responsibility Law) should resolve via local area mappings
instead of landing in unresolved.

Bug 2: The FOLIO /search/label API returns junk at score 90 for any
query.  The RestApiOntologyProvider must re-score candidates with
``compute_relevance_score`` (word-overlap) so labels with zero token
overlap score 0.0 and are filtered out.

Bug 3: The PlaceNameGate must recognise ``forums_and_venues`` as a
place branch so US state courts ("Arkansas State Courts") are demoted
instead of passing through at score 90.
"""

import httpx
import pytest
import respx

from sg_law_cookies.area_mappings import lookup_local_area
from sg_law_cookies.folio import FOLIO_API_BASE, resolve_topic
from sg_law_cookies.folio_resolve_adapter import (
    FORUMS_VENUES_BRANCH,
    _build_pipeline,
    _resolve_via_pipeline,
    clear_cache,
)
from sg_law_cookies.models import TopicExtraction


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _topic(**kw) -> TopicExtraction:
    return TopicExtraction(
        headline="h", summary="s", why_it_matters="w", significance="low", **kw
    )


def _owl_class(label: str, iri_id: str, alt_labels: list[str] | None = None) -> dict:
    return {
        "iri": f"{FOLIO_API_BASE}/{iri_id}",
        "label": label,
        "parent_class_of": [],
        "sub_class_of": [],
        "definition": None,
        "alternative_labels": alt_labels or [],
        "deprecated": False,
    }


def _path_payload(root_label: str) -> dict:
    return {"path": [{"iri": f"{FOLIO_API_BASE}/Rroot", "label": root_label, "id": "Rroot"}]}


# ── Bug 1: Local area mappings ────────────────────────────────────


def test_arbitration_law_resolves_locally():
    """Arbitration Law is not in FOLIO's area-of-law taxonomy but should
    resolve via the local area mappings, not land in unresolved."""
    topic = _topic(raw_areas=["Arbitration Law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert len(topic.folio_areas) == 1
    ref = topic.folio_areas[0]
    assert ref.preferred_label == "Arbitration Law"
    assert ref.branch == "sg_local_area"
    assert ref.confidence == 1.0
    assert topic.unresolved == []


def test_ethics_law_resolves_locally():
    topic = _topic(raw_areas=["Ethics and Professional Responsibility Law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert len(topic.folio_areas) == 1
    assert topic.folio_areas[0].preferred_label == "Ethics and Professional Responsibility Law"
    assert topic.folio_areas[0].branch == "sg_local_area"
    assert topic.unresolved == []


def test_unknown_area_still_unresolved():
    """An area not in FOLIO vocab AND not in local mappings still goes
    to unresolved."""
    topic = _topic(raw_areas=["Quantum Physics Law"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    assert topic.folio_areas == []
    assert "Quantum Physics Law" in topic.unresolved


def test_local_area_lookup_normalises():
    """The local area lookup normalises case/whitespace like sg_mappings."""
    ref = lookup_local_area("  arbitration  law ")
    assert ref is not None
    assert ref.preferred_label == "Arbitration Law"


def test_local_area_lookup_returns_copy():
    """Each lookup returns a fresh copy, not shared state."""
    a = lookup_local_area("Arbitration Law")
    b = lookup_local_area("Arbitration Law")
    assert a is not None and b is not None
    assert a is not b


# ── Bug 2: Word-overlap rescoring filters junk API scores ─────────


@respx.mock
def test_junk_api_score_filtered_by_word_overlap():
    """The FOLIO API returns "Arkansas State Courts" at score 90 for
    query "apparent bias".  After rescoring with compute_relevance_score,
    zero word overlap → score 0.0 → filtered out → unresolved."""
    # API returns junk: Arkansas State Courts at score 90
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [[_owl_class("Arkansas State Courts", "Rark"), 90.0]]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Forums and Venues")
    )
    topic = _topic(raw_concepts=["apparent bias"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    # No word overlap between "apparent bias" and "Arkansas State Courts"
    assert topic.folio_concepts == []
    assert "apparent bias" in topic.unresolved


@respx.mock
def test_real_match_passes_rescoring():
    """A genuine word-overlap match (not a junk hit) passes the rescoring
    filter.  "Arkansas State Courts" (zero overlap) is filtered out,
    while "Enforcement of Judgment" (exact match for that query) passes
    both rescoring and the gates."""
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [
            [_owl_class("Enforcement of Judgment", "Reoj"), 90.0],
            [_owl_class("Arkansas State Courts", "Rark"), 90.0],
        ]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Objectives")
    )
    topic = _topic(raw_concepts=["enforcement of judgment"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    # "Enforcement of Judgment" is an exact match → score 99 → passes.
    # "Arkansas State Courts" has zero overlap → filtered out.
    assert len(topic.folio_concepts) == 1
    assert topic.folio_concepts[0].preferred_label == "Enforcement of Judgment"
    assert topic.unresolved == []


@respx.mock
def test_industry_junk_filtered():
    """"Computing Infrastructure Providers, Data Processing..." returned
    for "Singapore International Commercial Court" has no word overlap
    and must be filtered out."""
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [
            [_owl_class("Computing Infrastructure Providers, Data Processing, Web Hosting, and Related Services Industry", "Rcomp"), 90.0],
            [_owl_class("Cross-Border Objective", "Rcross"), 90.0],
        ]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Objectives")
    )
    topic = _topic(raw_concepts=["Singapore International Commercial Court"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    # Neither label has content-word overlap with the query
    assert topic.folio_concepts == []
    assert "Singapore International Commercial Court" in topic.unresolved


# ── Bug 3: forums_and_venue branch recognised by PlaceNameGate ────


@respx.mock
def test_us_state_court_demoted_by_place_gate():
    """Even if a US state court somehow passes the word-overlap filter
    (e.g. query "Arkansas" matches "Arkansas State Courts"), the
    PlaceNameGate should demote it because forums_and_venues is now
    recognised as a place branch."""
    # "Arkansas" does share a token with "Arkansas State Courts"
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [[_owl_class("Arkansas State Courts", "Rark"), 90.0]]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Forums and Venues")
    )
    topic = _topic(raw_concepts=["Arkansas"])
    with httpx.Client() as client:
        resolve_topic(topic, client)
    # Place gate demotes forums_and_venues candidates below the score floor
    assert topic.folio_concepts == []
    assert "Arkansas" in topic.unresolved