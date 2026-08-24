"""Tests for the folio-resolve adapter (Phase 0 spike).

These tests use a small in-memory ontology injected via ``set_pipeline``
so they don't need network access or respx mocking. They verify the
adapter's FolioRef conversion, branch filtering, and the same SG-mapping
and area-vocab short-circuit behaviour as the legacy path.
"""

import httpx
import pytest

from folio_resolve import Concept, InMemoryOntology, MatchPipeline, PlaceNameGate

from sg_law_cookies.folio_resolve_adapter import (
    _candidate_to_folio_ref,
    _SCORE_SCALE,
    clear_cache,
    resolve_judgment_meta,
    resolve_legislation,
    resolve_topic,
    resolve_venue,
    set_pipeline,
)
from sg_law_cookies.folio import FOLIO_API_BASE
from sg_law_cookies.models import (
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    TopicExtraction,
)


@pytest.fixture(autouse=True)
def _clear():
    clear_cache()
    yield
    clear_cache()


# ── Ontology fixtures ─────────────────────────────────────────────


def _ontology():
    """Small ontology covering the test cases."""
    return InMemoryOntology([
        Concept(iri="R-arb", label="Arbitration Rules", branch="Service"),
        Concept(iri="R-cd", label="Constructive Dismissal", branch="Objectives"),
        Concept(iri="R-doc", label="Duty of Care", branch="Objectives"),
        Concept(iri="R-wisc", label="Supreme Court of Wisconsin", branch="forums_venues"),
        Concept(iri="R-fre", label="Federal Rules of Evidence", branch="legal_authorities"),
        Concept(iri="R-emp", label="Employment Law", branch="areas_of_law"),
    ])


def _pipeline():
    return MatchPipeline(
        ontology=_ontology(),
        place_gate=PlaceNameGate(
            extra_tokens={"singapore"},
            extra_markers=("republic of",),
        ),
        score_floor=60.0,
    )


@pytest.fixture
def pipeline():
    p = _pipeline()
    set_pipeline(p)
    return p


_DUMMY_CLIENT = httpx.Client()


def _topic(**kw) -> TopicExtraction:
    return TopicExtraction(
        headline="h", summary="s", why_it_matters="w", significance="low", **kw
    )


def _raw_ref(label: str) -> FolioRef:
    return FolioRef(iri=None, preferred_label=label, branch="unresolved", confidence=0.0)


# ── _candidate_to_folio_ref ───────────────────────────────────────


def test_candidate_to_folio_ref_normalises_score():
    from folio_resolve import MatchCandidate

    c = MatchCandidate(
        iri="R1", label="Test", score=88.0, branch="Objectives",
        extraction_path="label_search", surface_term="test",
        gated=False, gate_reason="not-a-place",
    )
    ref = _candidate_to_folio_ref(c)
    assert ref.iri == "R1"
    assert ref.preferred_label == "Test"
    assert ref.branch == "Objectives"
    assert ref.confidence == pytest.approx(0.88)


# ── resolve_topic: areas (closed vocab, no pipeline) ──────────────


def test_resolve_area_from_vocab_no_pipeline(pipeline):
    topic = _topic(raw_areas=["Employment Law"])
    resolve_topic(topic, _DUMMY_CLIENT)
    assert len(topic.folio_areas) == 1
    assert topic.folio_areas[0].preferred_label == "Employment Law"
    assert topic.folio_areas[0].branch == "areas_of_law"
    assert topic.folio_areas[0].confidence == 1.0


def test_resolve_area_unknown_label_unresolved(pipeline):
    topic = _topic(raw_areas=["Made Up Law"])
    resolve_topic(topic, _DUMMY_CLIENT)
    assert topic.folio_areas == []
    assert "Made Up Law" in topic.unresolved


# ── resolve_topic: entities (SG mappings short-circuit) ────────────


def test_resolve_entity_sg_mapping_short_circuits(pipeline):
    topic = _topic(raw_entities=["PDPC"])
    resolve_topic(topic, _DUMMY_CLIENT)
    assert len(topic.folio_entities) == 1
    assert topic.folio_entities[0].preferred_label == "Personal Data Protection Commission"
    assert topic.folio_entities[0].branch == "sg_local"
    assert topic.unresolved == []


# ── resolve_topic: concepts (pipeline) ─────────────────────────────


def test_resolve_concept_exact_match(pipeline):
    topic = _topic(raw_concepts=["Duty of Care"])
    resolve_topic(topic, _DUMMY_CLIENT)
    assert len(topic.folio_concepts) == 1
    ref = topic.folio_concepts[0]
    assert ref.iri == "R-doc"
    assert ref.preferred_label == "Duty of Care"
    assert ref.confidence == pytest.approx(0.99)
    assert topic.unresolved == []


def test_resolve_concept_word_order_invariant(pipeline):
    # "rules of arbitration" should match "Arbitration Rules"
    topic = _topic(raw_concepts=["rules of arbitration"])
    resolve_topic(topic, _DUMMY_CLIENT)
    assert len(topic.folio_concepts) == 1
    assert topic.folio_concepts[0].iri == "R-arb"
    assert topic.folio_concepts[0].preferred_label == "Arbitration Rules"


def test_resolve_concept_below_threshold_unresolved(pipeline):
    topic = _topic(raw_concepts=["completely unrelated nonsense term"])
    resolve_topic(topic, _DUMMY_CLIENT)
    assert topic.folio_concepts == []
    assert "completely unrelated nonsense term" in topic.unresolved


# ── resolve_venue ──────────────────────────────────────────────────


def test_resolve_venue_sg_court_short_circuits(pipeline):
    ref = resolve_venue(_DUMMY_CLIENT, "Court of Appeal")
    assert ref.preferred_label == "Court of Appeal of Singapore"
    assert ref.branch == "sg_local"
    assert ref.confidence == 1.0


def test_resolve_venue_pipeline_match(pipeline):
    # Venue resolution uses branch-filtered REST API search, not the
    # injected pipeline. With a mock client (no real API), it degrades
    # to unresolved. The SG local table catches known SG courts.
    import respx
    with respx.mock:
        respx.get(f"{FOLIO_API_BASE}/search/query").respond(json={"classes": []})
        ref = resolve_venue(httpx.Client(), "Supreme Court of Wisconsin")
    assert ref.iri is None
    assert ref.branch == "unresolved"


def test_resolve_venue_no_match_degrades(pipeline):
    ref = resolve_venue(_DUMMY_CLIENT, "Intergalactic Trade Tribunal")
    assert ref.iri is None
    assert ref.branch == "unresolved"


# ── resolve_legislation ───────────────────────────────────────────


def test_resolve_legislation_pipeline_match(pipeline):
    # Legislation resolution uses branch-filtered REST API search.
    # With a mock client (no real API), it degrades to unresolved.
    import respx
    with respx.mock:
        respx.get(f"{FOLIO_API_BASE}/search/query").respond(json={"classes": []})
        ref = resolve_legislation(httpx.Client(), "Federal Rules of Evidence")
    assert ref.iri is None
    assert ref.branch == "unresolved"


def test_resolve_legislation_sg_statute_unresolved(pipeline):
    ref = resolve_legislation(_DUMMY_CLIENT, "Personal Data Protection Act 2012")
    assert ref.iri is None
    assert ref.branch == "unresolved"


# ── resolve_judgment_meta ──────────────────────────────────────────


def test_resolve_judgment_meta_resolves_all_layers(pipeline):
    meta = JudgmentMeta(
        source_id="src-1",
        citation="[2026] SGCA 1",
        court=_raw_ref("Court of Appeal"),
        issues=[
            JudgmentIssue(
                question="Was a duty of care owed?",
                holding="Yes.",
                folio_concepts=[_raw_ref("duty of care"), _raw_ref("nonsense term")],
            )
        ],
        legislation=[_raw_ref("Civil Law Act 1909")],
    )
    out = resolve_judgment_meta(_DUMMY_CLIENT, meta)
    assert out is meta  # in place
    assert meta.court.preferred_label == "Court of Appeal of Singapore"
    assert meta.court.branch == "sg_local"

    concepts = meta.issues[0].folio_concepts
    assert len(concepts) == 2
    assert concepts[0].iri == "R-doc"
    assert concepts[0].preferred_label == "Duty of Care"
    assert concepts[1].iri is None
    assert concepts[1].preferred_label == "nonsense term"

    assert meta.legislation[0].iri is None
    assert meta.legislation[0].preferred_label == "Civil Law Act 1909"


def test_resolve_judgment_meta_leaves_resolved_refs_untouched(pipeline):
    court = FolioRef(iri="iri-court", preferred_label="Resolved", branch="forums_venues", confidence=1.0)
    concept = FolioRef(iri="iri-c", preferred_label="Estoppel", branch="objectives", confidence=0.9)
    leg = FolioRef(iri="iri-l", preferred_label="Some Rules", branch="legal_authorities", confidence=0.8)
    meta = JudgmentMeta(
        source_id="src-1",
        citation="[2026] SGCA 1",
        court=court,
        issues=[JudgmentIssue(question="q", folio_concepts=[concept])],
        legislation=[leg],
    )
    resolve_judgment_meta(_DUMMY_CLIENT, meta)
    assert meta.court == court
    assert meta.issues[0].folio_concepts == [concept]
    assert meta.legislation == [leg]


def test_resolve_judgment_meta_handles_missing_court(pipeline):
    meta = JudgmentMeta(source_id="src-1", citation="c", court=None)
    out = resolve_judgment_meta(_DUMMY_CLIENT, meta)
    assert out is not None
    assert out.court is None