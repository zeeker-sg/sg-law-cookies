"""Tests for the Zeeker Datasette client."""

from datetime import date

import httpx
import pytest
import respx

from sg_law_cookies.zeeker import BASE_URL, CatalogueEntry, ZeekerClient

METADATA = {
    "license": "CC-BY-4.0",
    "databases": {
        "sglawwatch": {"license": "CC-BY-4.0"},
        "zeeker-judgements": {"license": "CC-BY-4.0"},
        "pdpc": {"license": "All rights reserved"},
        "*": {},
    },
}

DATABASES = [{"name": "sglawwatch"}, {"name": "zeeker-judgements"}]

SGLAWWATCH_TABLES = {
    "tables": [
        {"name": "_zeeker_schemas", "hidden": False, "count": 4},
        {"name": "headlines", "hidden": False, "count": 764},
        {"name": "headlines_fts", "hidden": True, "count": 764},
        {"name": "commentaries", "hidden": False, "count": 153},
        {"name": "about_singapore_law", "hidden": False, "count": 45},
        {"name": "about_singapore_law_fragments", "hidden": False, "count": 2454},
        {"name": "schema_versions", "hidden": False, "count": 3},
        {"name": "metadata", "hidden": False, "count": 5},
    ]
}

JUDGEMENTS_TABLES = {
    "tables": [
        {"name": "judgments", "hidden": False, "count": None},
        {"name": "judgments_fragments", "hidden": False, "count": None},
    ]
}

HEADLINE_ROW = {
    "id": "abc123",
    "category": "Straits Times",
    "title": "Court clarifies duty of care",
    "source_link": "https://www.singaporelawwatch.sg/Headlines/court-clarifies",
    "author": "Straits Times: Jane Tan",
    "date": "2026-06-08T00:01:00",
    "summary": "A short summary.",
    "text": "Full article text.",
    "imported_on": "2026-06-08T05:00:40.174846",
}


def make_client() -> ZeekerClient:
    return ZeekerClient(min_interval=0.0, backoff=0.0)


def mock_metadata() -> None:
    respx.get(f"{BASE_URL}/-/metadata.json").respond(json=METADATA)


@respx.mock
def test_discover_catalogue_skips_non_content_tables():
    mock_metadata()
    respx.get(f"{BASE_URL}/-/databases.json").respond(json=DATABASES)
    respx.get(f"{BASE_URL}/sglawwatch.json").respond(json=SGLAWWATCH_TABLES)
    respx.get(f"{BASE_URL}/zeeker-judgements.json").respond(json=JUDGEMENTS_TABLES)

    entries = make_client().discover_catalogue()

    assert entries == [
        CatalogueEntry("sglawwatch", "headlines", "CC-BY-4.0", 764),
        CatalogueEntry("sglawwatch", "commentaries", "CC-BY-4.0", 153),
        CatalogueEntry("sglawwatch", "about_singapore_law", "CC-BY-4.0", 45),
        CatalogueEntry("zeeker-judgements", "judgments", "CC-BY-4.0", None),
    ]


@respx.mock
def test_fetch_new_rows_paginates_and_filters_by_watermark():
    row1 = {"id": "r1", "imported_on": "2026-06-04T17:49:07"}
    row2 = {"id": "r2", "imported_on": "2026-06-05T05:00:00"}
    row3 = {"id": "r3", "imported_on": "2026-06-08T05:00:40"}
    route = respx.get(f"{BASE_URL}/sglawwatch/headlines.json").mock(
        side_effect=[
            httpx.Response(200, json={"rows": [row1, row2], "next": "tok1"}),
            httpx.Response(200, json={"rows": [row3], "next": None}),
        ]
    )

    rows = make_client().fetch_new_rows(
        "sglawwatch", "headlines", since="2026-06-01T00:00:00", limit=10
    )

    assert [r["id"] for r in rows] == ["r1", "r2", "r3"]
    timestamps = [r["imported_on"] for r in rows]
    assert timestamps == sorted(timestamps)  # ascending, watermark-safe

    first = route.calls[0].request.url.params
    assert first["_sort"] == "imported_on"
    assert first["imported_on__gt"] == "2026-06-01T00:00:00"
    assert first["_shape"] == "objects"
    second = route.calls[1].request.url.params
    assert second["_next"] == "tok1"
    assert second["imported_on__gt"] == "2026-06-01T00:00:00"


@respx.mock
def test_fetch_new_rows_without_since_omits_filter():
    route = respx.get(f"{BASE_URL}/sglawwatch/headlines.json").respond(
        json={"rows": [], "next": None}
    )

    rows = make_client().fetch_new_rows("sglawwatch", "headlines")

    assert rows == []
    assert "imported_on__gt" not in route.calls[0].request.url.params


@respx.mock
def test_fetch_new_rows_respects_limit():
    route = respx.get(f"{BASE_URL}/sglawwatch/headlines.json").respond(
        json={"rows": [{"id": "r1"}, {"id": "r2"}], "next": "tok1"}
    )

    rows = make_client().fetch_new_rows("sglawwatch", "headlines", limit=2)

    assert len(rows) == 2
    assert len(route.calls) == 1
    assert route.calls[0].request.url.params["_size"] == "2"


@respx.mock
def test_fetch_new_rows_uses_created_at_for_judgments():
    route = respx.get(f"{BASE_URL}/zeeker-judgements/judgments.json").respond(
        json={"rows": [], "next": None}
    )

    make_client().fetch_new_rows("zeeker-judgements", "judgments", since="2026-06-01")

    params = route.calls[0].request.url.params
    assert params["_sort"] == "created_at"
    assert params["created_at__gt"] == "2026-06-01"


@respx.mock
def test_retries_on_429_then_succeeds():
    route = respx.get(f"{BASE_URL}/sglawwatch/headlines.json").mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json={"rows": [], "next": None}),
        ]
    )

    rows = make_client().fetch_new_rows("sglawwatch", "headlines")

    assert rows == []
    assert len(route.calls) == 2


@respx.mock
def test_to_raw_item_headlines():
    mock_metadata()

    item = make_client().to_raw_item("sglawwatch", "headlines", HEADLINE_ROW)

    assert item.source_url == "https://www.singaporelawwatch.sg/Headlines/court-clarifies"
    assert item.zeeker_url == f"{BASE_URL}/sglawwatch/headlines/abc123"
    assert item.title == "Court clarifies duty of care"
    assert item.raw_text == "Full article text."
    assert item.date == date(2026, 6, 8)
    assert item.source_id == "sglawwatch"
    assert item.item_type == "news"
    assert item.license == "CC-BY-4.0"


@respx.mock
def test_to_raw_item_judgment():
    mock_metadata()
    row = {
        "id": "j1",
        "citation": "[2026] SGHC 34",
        "case_name": "Tan v Lim",
        "decision_date": "2026-05-20",
        "court": "High Court",
        "source_url": "https://www.elitigation.sg/gd/s/2026_SGHC_34",
        "pdf_url": "https://example.org/j1.pdf",
        "content_text": "Judgment text.",
        "court_summary": None,
        "summary": "AI summary.",
        "created_at": "2026-06-01T08:00:00",
    }

    item = make_client().to_raw_item("zeeker-judgements", "judgments", row)

    assert item.item_type == "judgment"
    assert item.source_url == "https://www.elitigation.sg/gd/s/2026_SGHC_34"
    assert item.zeeker_url == f"{BASE_URL}/zeeker-judgements/judgments/j1"
    assert item.title == "Tan v Lim"
    assert item.raw_text == "Judgment text."
    assert item.date == date(2026, 5, 20)
    assert item.license == "CC-BY-4.0"


@respx.mock
def test_to_raw_item_enforcement_decision():
    mock_metadata()
    row = {
        "id": "e1",
        "title": "Breach of the Protection Obligation by Acme",
        "organisation": "Acme Pte Ltd",
        "decision_type": "Financial Penalty",
        "decision_date": "2026-04-10",
        "decision_url": "https://www.pdpc.gov.sg/all-commissions-decisions/acme",
        "penalty_amount": 20000,
        "summary": "Decision summary.",
        "pdf_url": "https://example.org/e1.pdf",
        "imported_on": "2026-04-12T01:00:00",
    }

    item = make_client().to_raw_item("pdpc", "enforcement_decisions", row)

    assert item.item_type == "judgment"
    assert item.source_url == "https://www.pdpc.gov.sg/all-commissions-decisions/acme"
    assert item.raw_text == "Decision summary."
    assert item.date == date(2026, 4, 10)
    assert item.license == "All rights reserved"


@respx.mock
def test_to_raw_item_falls_back_to_watermark_date():
    mock_metadata()
    row = dict(HEADLINE_ROW, date=None)

    item = make_client().to_raw_item("sglawwatch", "headlines", row)

    assert item.date == date(2026, 6, 8)


@pytest.mark.live
def test_live_fetch_headlines():
    client = ZeekerClient()
    entries = client.discover_catalogue()
    assert ("sglawwatch", "headlines") in {(e.database, e.table) for e in entries}

    rows = client.fetch_new_rows("sglawwatch", "headlines", since=None, limit=2)
    assert len(rows) == 2
    for row in rows:
        item = client.to_raw_item("sglawwatch", "headlines", row)
        assert item.source_url.startswith("http")
        assert item.item_type == "news"
        assert item.license == "CC-BY-4.0"
        assert item.title
