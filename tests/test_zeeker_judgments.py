"""Tests for judgment-table row mapping (zeeker-judgements + pdpc).

Column names verified against live data.zeeker.sg (2026-06-12):

zeeker-judgements/judgments: id, citation, case_name, case_numbers,
    decision_date, court, subject_tags, source_url, pdf_url,
    content_text, court_summary, summary, created_at, has_content,
    has_court_summary, fragment_count, extracted_at,
    summary_generated_at. Sortable by created_at (no imported_on).

pdpc/enforcement_decisions: id, title, organisation, decision_type,
    decision_date, decision_url, penalty_amount, summary, pdf_url,
    imported_on. Sortable by imported_on.
"""

from datetime import date

import pytest
import httpx
import respx

from sg_law_cookies.zeeker import (
    BASE_URL,
    COURT_SUMMARY_EXTRA_LIMIT,
    ZeekerClient,
    timestamp_column,
)

METADATA = {
    "license": "CC-BY-4.0",
    "databases": {
        "zeeker-judgements": {"license": "CC-BY-4.0"},
        "pdpc": {"license": "All rights reserved"},
        "*": {},
    },
}

JUDGMENT_ROW = {
    "rowid": 10612,
    "id": "3e8b329ece4f",
    "citation": "[2026] SGDC 190",
    "case_name": "CARLSON CLARK SMITH v GOH HIN CALM & 5 Ors",
    "case_numbers": "DC/OC 339/2024 ( DC/RA 25/2026 )",
    "decision_date": "2026-06-08",
    "court": "SGDC",
    "subject_tags": '["Civil Procedure — Striking out"]',
    "source_url": "https://www.elitigation.sg/gd/s/2026_SGDC_190",
    "pdf_url": "https://www.elitigation.sg/gd/gd/%5B2026%5D%20SGDC%20190/pdf",
    "content_text": "Full judgment text.",
    "court_summary": "Court-issued summary.",
    "summary": "AI summary.",
    "created_at": "2026-06-08T05:07:04.242794",
    "has_content": 1,
    "has_court_summary": 1,
}

PDPC_ROW = {
    "rowid": 300,
    "id": "c2a26ba3-e0de-4528-8672-a0690c3c58e1",
    "title": "Breach of the Protection Obligation by Acme",
    "organisation": "Acme Pte Ltd",
    "decision_type": "Financial Penalty",
    "decision_date": "2026-04-10",
    "decision_url": "https://www.pdpc.gov.sg/all-commissions-decisions/acme",
    "penalty_amount": 20000,
    "summary": "Acme failed to protect personal data.",
    "pdf_url": "https://www.pdpc.gov.sg/files/acme.pdf",
    "imported_on": "2026-04-12T01:00:00.000000+00:00",
}


def make_client() -> ZeekerClient:
    return ZeekerClient(min_interval=0.0, backoff=0.0)


def mock_metadata() -> None:
    respx.get(f"{BASE_URL}/-/metadata.json").respond(json=METADATA)


def test_timestamp_columns_match_live_schema():
    # judgments has created_at only (imported_on 500s when sorted on, live).
    assert timestamp_column("zeeker-judgements", "judgments") == "created_at"
    assert timestamp_column("pdpc", "enforcement_decisions") == "imported_on"


@respx.mock
def test_judgment_row_maps_content_text_and_extras():
    mock_metadata()

    item = make_client().to_raw_item("zeeker-judgements", "judgments", JUDGMENT_ROW)

    assert item is not None
    assert item.item_type == "judgment"
    assert item.source_url == "https://www.elitigation.sg/gd/s/2026_SGDC_190"
    assert item.zeeker_url == f"{BASE_URL}/zeeker-judgements/judgments/3e8b329ece4f"
    assert item.title == "CARLSON CLARK SMITH v GOH HIN CALM & 5 Ors"
    assert item.raw_text == "Full judgment text."
    assert item.date == date(2026, 6, 8)  # decision_date, not created_at
    assert item.source_id == "zeeker-judgements"
    assert item.license == "CC-BY-4.0"
    assert item.extras["text_source"] == "content_text"
    assert item.extras["citation"] == "[2026] SGDC 190"
    assert item.extras["court"] == "SGDC"
    assert item.extras["court_summary"] == "Court-issued summary."
    assert item.extras["subject_tags"] == '["Civil Procedure — Striking out"]'
    assert item.extras["pdf_url"] == JUDGMENT_ROW["pdf_url"]


@respx.mock
def test_judgment_row_falls_back_to_court_summary_then_summary():
    mock_metadata()
    client = make_client()

    row = dict(JUDGMENT_ROW, content_text="", has_content=0)
    item = client.to_raw_item("zeeker-judgements", "judgments", row)
    assert item is not None
    assert item.raw_text == "Court-issued summary."
    assert item.extras["text_source"] == "court_summary"

    row = dict(JUDGMENT_ROW, content_text=None, court_summary="", has_content=0)
    item = client.to_raw_item("zeeker-judgements", "judgments", row)
    assert item is not None
    assert item.raw_text == "AI summary."
    assert item.extras["text_source"] == "summary"
    assert "court_summary" not in item.extras  # empty values stay out of extras


@respx.mock
def test_judgment_row_without_text_is_skipped():
    mock_metadata()
    row = dict(
        JUDGMENT_ROW, content_text="", court_summary="", summary=None, has_content=0
    )

    item = make_client().to_raw_item("zeeker-judgements", "judgments", row)

    assert item is None


@respx.mock
def test_judgment_court_summary_extra_is_truncated():
    mock_metadata()
    row = dict(JUDGMENT_ROW, court_summary="x" * 5000)

    item = make_client().to_raw_item("zeeker-judgements", "judgments", row)

    assert item is not None
    assert len(item.extras["court_summary"]) == COURT_SUMMARY_EXTRA_LIMIT


@respx.mock
def test_judgment_row_missing_subject_tags_omitted_from_extras():
    mock_metadata()
    row = dict(JUDGMENT_ROW, subject_tags=None)

    item = make_client().to_raw_item("zeeker-judgements", "judgments", row)

    assert item is not None
    assert "subject_tags" not in item.extras


@respx.mock
def test_pdpc_row_maps_decision_url_and_extras():
    mock_metadata()

    item = make_client().to_raw_item("pdpc", "enforcement_decisions", PDPC_ROW)

    assert item is not None
    assert item.item_type == "judgment"
    assert item.source_url == "https://www.pdpc.gov.sg/all-commissions-decisions/acme"
    assert item.zeeker_url == (
        f"{BASE_URL}/pdpc/enforcement_decisions/{PDPC_ROW['id']}"
    )
    assert item.title == "Breach of the Protection Obligation by Acme"
    assert item.raw_text == "Acme failed to protect personal data."
    assert item.date == date(2026, 4, 10)  # decision_date, not imported_on
    assert item.license == "All rights reserved"
    assert item.extras["text_source"] == "summary"
    assert item.extras["organisation"] == "Acme Pte Ltd"
    assert item.extras["decision_type"] == "Financial Penalty"
    assert item.extras["penalty_amount"] == "20000"
    assert item.extras["pdf_url"] == "https://www.pdpc.gov.sg/files/acme.pdf"


@respx.mock
def test_pdpc_row_without_summary_is_skipped():
    mock_metadata()
    row = dict(PDPC_ROW, summary="")

    item = make_client().to_raw_item("pdpc", "enforcement_decisions", row)

    assert item is None


@respx.mock
def test_pdpc_row_null_penalty_omitted_from_extras():
    mock_metadata()
    row = dict(PDPC_ROW, penalty_amount=None, pdf_url="")

    item = make_client().to_raw_item("pdpc", "enforcement_decisions", row)

    assert item is not None
    assert "penalty_amount" not in item.extras
    assert "pdf_url" not in item.extras


@pytest.mark.live
def test_live_fetch_judgments():
    client = ZeekerClient()
    rows = client.fetch_new_rows("zeeker-judgements", "judgments", since=None, limit=2)
    assert len(rows) == 2

    items = [
        client.to_raw_item("zeeker-judgements", "judgments", row) for row in rows
    ]
    items = [item for item in items if item is not None]
    assert items, "expected at least one judgment row with usable text"
    for item in items:
        assert item.item_type == "judgment"
        # original document URL, never a data.zeeker.sg URL
        assert item.source_url.startswith("http")
        assert "data.zeeker.sg" not in item.source_url
        assert item.zeeker_url.startswith(f"{BASE_URL}/zeeker-judgements/judgments/")
        assert item.raw_text
        assert item.extras["text_source"] in {"content_text", "court_summary", "summary"}
        assert item.extras["citation"]
        assert item.extras["court"]
        assert len(item.extras.get("court_summary", "")) <= COURT_SUMMARY_EXTRA_LIMIT


@respx.mock
def test_watermark_timeout_falls_back_to_rowid_walk():
    # Regression: Datasette times out (400) filtering/sorting the unindexed
    # created_at column on the large judgments table; client must fall back
    # to a rowid-descending walk with client-side filtering.
    def router(request):
        params = dict(request.url.params)
        if "created_at__gt" in params:
            return httpx.Response(400, json={"ok": False, "error": "SQL query took too long"})
        assert params.get("_sort_desc") == "rowid"
        return httpx.Response(200, json={"rows": [
            {"rowid": 12, "id": "c", "created_at": "2026-06-08T05:00:00"},
            {"rowid": 11, "id": "b", "created_at": "2026-06-05T05:00:00"},
            {"rowid": 10, "id": "a", "created_at": "2026-05-20T05:00:00"},  # behind watermark
        ], "next": None})

    respx.get(url__regex=r".*judgments\.json.*").mock(side_effect=router)
    client = ZeekerClient()
    rows = client.fetch_new_rows("zeeker-judgements", "judgments", since="2026-06-01T00:00:00", limit=10)
    assert [r["id"] for r in rows] == ["b", "c"]  # ascending, watermark row excluded
