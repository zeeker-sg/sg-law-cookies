"""Tests for judgment-specific FOLIO resolution (PRD 4.2 step 5)."""

import httpx
import pytest
import respx

from sg_law_cookies import folio
from sg_law_cookies.folio import (
    FOLIO_API_BASE,
    FORUMS_VENUES_BRANCH,
    LEGAL_AUTHORITIES_BRANCH,
    resolve_judgment_meta,
    resolve_legislation,
    resolve_venue,
)
from sg_law_cookies.models import FolioRef, JudgmentIssue, JudgmentMeta


@pytest.fixture(autouse=True)
def _clear_cache():
    folio.clear_cache()
    yield
    folio.clear_cache()


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


def _raw_ref(label: str) -> FolioRef:
    """Placeholder shape the extraction step uses for unresolved raw labels."""
    return FolioRef(iri=None, preferred_label=label, branch="unresolved", confidence=0.0)


def _meta(**kwargs) -> JudgmentMeta:
    return JudgmentMeta(source_id="src-1", citation="[2026] SGCA 1", **kwargs)


# ── resolve_venue ────────────────────────────────────────────────


@respx.mock
def test_resolve_venue_sg_courts_short_circuit_api():
    route = respx.get(url__regex=rf"{FOLIO_API_BASE}/.*").respond(json={"classes": []})
    cases = {
        "Court of Appeal": "Court of Appeal of Singapore",
        "Court of Appeal of the Republic of Singapore": "Court of Appeal of Singapore",
        "General Division of the High Court": "High Court of Singapore",
        "Appellate Division of the High Court": "High Court of Singapore",
        "State Courts": "State Courts",
        "Family Justice Courts": "Family Justice Courts",
        "SICC": "Singapore International Commercial Court",
        "PDPC": "Personal Data Protection Commission",
    }
    with httpx.Client() as client:
        for query, expected in cases.items():
            ref = resolve_venue(client, query)
            assert ref.preferred_label == expected, query
            assert ref.branch == "sg_local"
            assert ref.confidence == 1.0
    assert route.call_count == 0


@respx.mock
def test_resolve_venue_uses_forums_venues_branch():
    route = respx.get(
        f"{FOLIO_API_BASE}/search/query", params={"branch": FORUMS_VENUES_BRANCH}
    ).respond(json={"classes": [_owl_class("Supreme Court of Wisconsin", "Rwisc")], "properties": []})
    with httpx.Client() as client:
        ref = resolve_venue(client, "Supreme Court of Wisconsin")
    assert route.called
    assert ref.iri == f"{FOLIO_API_BASE}/Rwisc"
    assert ref.preferred_label == "Supreme Court of Wisconsin"
    assert ref.branch == FORUMS_VENUES_BRANCH
    assert ref.confidence == 1.0


@respx.mock
def test_resolve_venue_no_match_degrades_to_placeholder():
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(json={"classes": [], "properties": []})
    with httpx.Client() as client:
        ref = resolve_venue(client, "Intergalactic Trade Tribunal")
    assert ref.iri is None
    assert ref.preferred_label == "Intergalactic Trade Tribunal"
    assert ref.branch == "unresolved"
    assert ref.confidence == 0.0


@respx.mock
def test_resolve_venue_api_failure_degrades_not_raises():
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(500)
    with httpx.Client() as client:
        ref = resolve_venue(client, "Some Foreign Court")
    assert ref.iri is None
    assert ref.branch == "unresolved"


# ── resolve_legislation ──────────────────────────────────────────


@respx.mock
def test_resolve_legislation_sg_statute_lands_unresolved():
    # Expected for Singapore statutes: FOLIO has no concept, label is kept.
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(json={"classes": [], "properties": []})
    with httpx.Client() as client:
        ref = resolve_legislation(client, "Personal Data Protection Act 2012")
    assert ref.iri is None
    assert ref.preferred_label == "Personal Data Protection Act 2012"
    assert ref.branch == "unresolved"
    assert ref.confidence == 0.0


@respx.mock
def test_resolve_legislation_uses_legal_authorities_branch():
    route = respx.get(
        f"{FOLIO_API_BASE}/search/query", params={"branch": LEGAL_AUTHORITIES_BRANCH}
    ).respond(
        json={"classes": [_owl_class("Federal Rules of Evidence", "Rfre")], "properties": []}
    )
    with httpx.Client() as client:
        ref = resolve_legislation(client, "Federal Rules of Evidence")
    assert route.called
    assert ref.iri == f"{FOLIO_API_BASE}/Rfre"
    assert ref.branch == LEGAL_AUTHORITIES_BRANCH
    assert ref.confidence == 1.0


@respx.mock
def test_resolve_legislation_api_failure_degrades_not_raises():
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(500)
    with httpx.Client() as client:
        ref = resolve_legislation(client, "Companies Act 1967")
    assert ref.iri is None
    assert ref.branch == "unresolved"


# ── resolve_judgment_meta ────────────────────────────────────────


@respx.mock
def test_resolve_judgment_meta_resolves_all_layers():
    # Issue concepts go through /search/label (all branches) + taxonomy path.
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(
        json={"results": [[_owl_class("Duty of Care", "Rdoc"), 95.0]]}
    )
    respx.get(url__regex=rf"{FOLIO_API_BASE}/taxonomy/tree/path/.*").respond(
        json=_path_payload("Objectives")
    )
    # Legislation goes through branch-filtered /search/query — no FOLIO match.
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(json={"classes": [], "properties": []})

    meta = _meta(
        court=_raw_ref("Court of Appeal"),
        issues=[
            JudgmentIssue(
                question="Was a duty of care owed?",
                holding="Yes.",
                folio_concepts=[_raw_ref("duty of care"), _raw_ref("zorbulent quasimodality")],
            )
        ],
        legislation=[_raw_ref("Civil Law Act 1909")],
    )
    with httpx.Client() as client:
        out = resolve_judgment_meta(client, meta)

    assert out is meta  # resolved in place
    assert meta.court is not None
    assert meta.court.preferred_label == "Court of Appeal of Singapore"
    assert meta.court.branch == "sg_local"

    concepts = meta.issues[0].folio_concepts
    assert len(concepts) == 2
    assert concepts[0].iri == f"{FOLIO_API_BASE}/Rdoc"
    assert concepts[0].preferred_label == "Duty of Care"
    assert concepts[0].branch == "objectives"
    assert concepts[0].confidence == 1.0
    # Unresolvable concept keeps its raw label as a placeholder, not dropped.
    assert concepts[1].iri is None
    assert concepts[1].preferred_label == "zorbulent quasimodality"
    assert concepts[1].branch == "unresolved"

    assert len(meta.legislation) == 1
    assert meta.legislation[0].iri is None
    assert meta.legislation[0].preferred_label == "Civil Law Act 1909"
    assert meta.legislation[0].branch == "unresolved"


@respx.mock
def test_resolve_judgment_meta_leaves_resolved_refs_untouched():
    route = respx.get(url__regex=rf"{FOLIO_API_BASE}/.*").respond(json={"classes": []})
    court = FolioRef(iri="iri-court", preferred_label="Resolved Court", branch="forums_venues", confidence=1.0)
    concept = FolioRef(iri="iri-c", preferred_label="Estoppel", branch="objectives", confidence=0.9)
    leg = FolioRef(iri="iri-l", preferred_label="Some Rules", branch="legal_authorities", confidence=0.8)
    meta = _meta(
        court=court,
        issues=[JudgmentIssue(question="q", folio_concepts=[concept])],
        legislation=[leg],
    )
    with httpx.Client() as client:
        resolve_judgment_meta(client, meta)
    assert route.call_count == 0
    assert meta.court == court
    assert meta.issues[0].folio_concepts == [concept]
    assert meta.legislation == [leg]


@respx.mock
def test_resolve_judgment_meta_handles_missing_court():
    respx.get(url__regex=rf"{FOLIO_API_BASE}/.*").respond(json={"classes": [], "results": []})
    meta = _meta(court=None)
    with httpx.Client() as client:
        resolve_judgment_meta(client, meta)
    assert meta.court is None


@respx.mock
def test_resolve_judgment_meta_never_raises_on_api_failure():
    respx.get(url__regex=rf"{FOLIO_API_BASE}/.*").respond(500)
    meta = _meta(
        court=_raw_ref("Dispute Resolution Forum of Atlantis"),
        issues=[JudgmentIssue(question="q", folio_concepts=[_raw_ref("unjust enrichment")])],
        legislation=[_raw_ref("Sea Law Act")],
    )
    with httpx.Client() as client:
        resolve_judgment_meta(client, meta)  # must not raise
    assert meta.court is not None and meta.court.iri is None
    assert meta.court.branch == "unresolved"
    assert meta.issues[0].folio_concepts[0].iri is None
    assert meta.issues[0].folio_concepts[0].preferred_label == "unjust enrichment"
    assert meta.legislation[0].iri is None


# ── live ─────────────────────────────────────────────────────────


@pytest.mark.live
def test_live_resolve_court_of_appeal():
    with httpx.Client(timeout=30) as client:
        # Live FOLIO probe: the forums_venues branch filter is valid and
        # returns matches for "Court of Appeal" — but none are Singapore
        # courts (only US state courts, e.g. "Washington Court of Appeals"),
        # which is exactly why the local SG table must win.
        results = folio._search_branch(client, "Court of Appeal", FORUMS_VENUES_BRANCH)
        assert results, "forums_venues branch filter returned nothing live"
        assert all("singapore" not in r.label.lower() for r in results)

        ref = resolve_venue(client, "Court of Appeal")
    assert ref.preferred_label == "Court of Appeal of Singapore"
    assert ref.branch == "sg_local"
    assert ref.confidence == 1.0


def test_generic_sg_court_names_resolve_locally_not_to_us_courts():
    # Regression: live smoke test resolved "District Court" to
    # "U.S. District Court - D. Oregon" via FOLIO substring match.
    import httpx
    from sg_law_cookies.folio import resolve_venue

    for name in ["District Court", "Magistrate's Court", "Family Court", "SGDC"]:
        ref = resolve_venue(httpx.Client(), name)  # no API call: local table hit
        assert ref.branch == "sg_local", name
        assert ref.confidence == 1.0, name
