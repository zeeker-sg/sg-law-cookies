"""End-to-end tests for the news pipeline, run loop, and CLI."""

import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import respx

from sg_law_cookies import db, folio
from sg_law_cookies.cli import main
from sg_law_cookies.folio import FOLIO_API_BASE
from sg_law_cookies.models import RawItem
from sg_law_cookies.pipeline import PipelineError, process_news, run_source
from sg_law_cookies.zeeker import BASE_URL, ZeekerClient

METADATA = {
    "license": "CC-BY-4.0",
    "databases": {"sglawwatch": {"license": "CC-BY-4.0"}, "*": {}},
}

ROW1 = {
    "id": "h1",
    "category": "Straits Times",
    "title": "EP salary floor to rise",
    "source_link": "https://www.singaporelawwatch.sg/Headlines/ep-salary-floor",
    "author": "Jane Tan",
    "date": "2026-06-08T00:01:00",
    "summary": "Short summary one.",
    "text": "MOM announced the EP qualifying salary will rise to SGD 6,000 from Jan 2027.",
    "imported_on": "2026-06-08T05:00:00.000001",
}

ROW2 = {
    "id": "h2",
    "category": "Business Times",
    "title": "PDPC fines retailer",
    "source_link": "https://www.singaporelawwatch.sg/Headlines/pdpc-fines-retailer",
    "author": "Lim Wei",
    "date": "2026-06-09T00:01:00",
    "summary": "Short summary two.",
    "text": "The PDPC imposed a financial penalty on a retailer for a data breach.",
    "imported_on": "2026-06-09T05:00:00.000002",
}


def topic_dict(headline: str = "EP salary floor rises to SGD 6,000", **overrides) -> dict:
    topic = {
        "headline": headline,
        "summary": "MOM will raise the EP qualifying salary. New applications from Jan 2027 are affected.",
        "why_it_matters": "Clients hiring foreign professionals must budget for the higher floor.",
        "significance": "high",
        "raw_areas": ["Employment"],
        "raw_entities": ["PDPC"],
        "raw_concepts": ["xyzzy doctrine"],
    }
    topic.update(overrides)
    return topic


class StubAnthropic:
    """Stands in for anthropic.Anthropic; returns queued topic lists."""

    def __init__(self, topics_per_call: list[list[dict]]):
        self._queue = list(topics_per_call)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        topics = self._queue.pop(0) if self._queue else []
        block = SimpleNamespace(
            type="tool_use", name="record_topics", input={"topics": topics}
        )
        return SimpleNamespace(content=[block], stop_reason="tool_use")


@pytest.fixture(autouse=True)
def _clear_folio_cache():
    folio.clear_cache()
    yield
    folio.clear_cache()


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "cookies.db")
    yield connection
    connection.close()


def make_zeeker_client() -> ZeekerClient:
    return ZeekerClient(min_interval=0.0, backoff=0.0)


def make_raw_item(url: str = "https://example.org/a", **overrides) -> RawItem:
    fields = {
        "source_url": url,
        "zeeker_url": f"{BASE_URL}/sglawwatch/headlines/x1",
        "title": "EP salary floor to rise",
        "raw_text": "MOM announced the EP qualifying salary will rise.",
        "date": date(2026, 6, 8),
        "source_id": "sglawwatch",
        "item_type": "news",
        "license": "CC-BY-4.0",
    }
    fields.update(overrides)
    return RawItem(**fields)


def mock_folio() -> None:
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(
        json={
            "classes": [
                {
                    "iri": f"{FOLIO_API_BASE}/R8pNPutX0TPDLtNqHV",
                    "label": "Employment Law",
                    "parent_class_of": [],
                }
            ]
        }
    )
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(json={"results": []})


def mock_zeeker_metadata() -> None:
    respx.get(f"{BASE_URL}/-/metadata.json").respond(json=METADATA)


# ── process_news ─────────────────────────────────────────────────────


@respx.mock
def test_process_news_extracts_resolves_and_stores(conn):
    mock_folio()
    stub = StubAnthropic([[topic_dict()]])

    with httpx.Client() as folio_client:
        cookies = process_news(make_raw_item(), conn, stub, folio_client)

    assert len(cookies) == 1
    cookie = cookies[0]
    assert cookie.headline == "EP salary floor rises to SGD 6,000"
    assert cookie.significance == "high"
    # area resolved via FOLIO (substring match >= threshold)
    assert cookie.folio_areas[0].preferred_label == "Employment Law"
    assert cookie.folio_areas[0].branch == "areas_of_law"
    # entity resolved via local Singapore mapping, no API call
    assert cookie.folio_entities[0].branch == "sg_local"
    # concept unresolved, recorded for review
    assert "xyzzy doctrine" in cookie.unresolved
    assert "xyzzy doctrine" in [t for t, _, _ in db.list_unresolved_terms(conn)]

    stored = db.get_cookie(conn, cookie.id)
    assert stored is not None
    assert len(stored.source_ids) == 1
    source = db.get_source(conn, stored.source_ids[0])
    assert source.source_url == "https://example.org/a"
    assert source.token_count > 0


@respx.mock
def test_process_news_same_url_skips_llm_and_reuses_cookies(conn):
    mock_folio()
    stub = StubAnthropic([[topic_dict()]])
    raw_item = make_raw_item()

    with httpx.Client() as folio_client:
        first = process_news(raw_item, conn, stub, folio_client)
        second = process_news(raw_item, conn, stub, folio_client)

    assert len(stub.calls) == 1  # no second LLM call
    assert [c.id for c in second] == [c.id for c in first]
    assert len(db.find_recent_cookies(conn, 1)) == 1


@respx.mock
def test_duplicate_headline_flagged_and_corroborates_original(conn):
    mock_folio()
    stub = StubAnthropic([[topic_dict("Same story")], [topic_dict("Same story")]])

    with httpx.Client() as folio_client:
        (first,) = process_news(make_raw_item("https://example.org/a"), conn, stub, folio_client)
        (second,) = process_news(make_raw_item("https://example.org/b"), conn, stub, folio_client)

    assert second.is_duplicate
    assert second.duplicate_of == first.id
    original = db.get_cookie(conn, first.id)
    assert len(original.source_ids) == 2  # corroborating source linked


# ── run_source ───────────────────────────────────────────────────────


@respx.mock
def test_run_source_end_to_end_and_idempotent_rerun(conn):
    mock_folio()
    mock_zeeker_metadata()
    headlines = respx.get(f"{BASE_URL}/sglawwatch/headlines.json").mock(
        side_effect=[
            httpx.Response(200, json={"rows": [ROW1, ROW2], "next": None}),
            httpx.Response(200, json={"rows": [], "next": None}),
        ]
    )
    stub = StubAnthropic(
        [[topic_dict("Cookie A")], [topic_dict("Cookie B", significance="medium")]]
    )

    with httpx.Client() as folio_client:
        result = run_source(
            conn, make_zeeker_client(), stub, folio_client, "sglawwatch/headlines", limit=10
        )

        assert result.processed == 2
        assert sorted(c.headline for c in result.cookies) == ["Cookie A", "Cookie B"]
        assert db.get_watermark(conn, "sglawwatch", "headlines") == ROW2["imported_on"]
        assert len(db.find_recent_cookies(conn, 1)) == 2
        registry = db.list_registry(conn)
        assert [(e.zeeker_db, e.table, e.active) for e in registry] == [
            ("sglawwatch", "headlines", True)
        ]

        # re-run: fetches strictly after the stored watermark, finds nothing
        rerun = run_source(
            conn, make_zeeker_client(), stub, folio_client, "sglawwatch/headlines", limit=10
        )

    assert rerun.processed == 0
    assert len(db.find_recent_cookies(conn, 1)) == 2  # no duplicate cookies
    assert len(stub.calls) == 2  # no further LLM calls
    second_fetch = headlines.calls[1].request.url.params
    assert second_fetch["imported_on__gt"] == ROW2["imported_on"]


@respx.mock
def test_run_source_dry_run_makes_no_writes(conn):
    mock_zeeker_metadata()
    respx.get(f"{BASE_URL}/sglawwatch/headlines.json").respond(
        json={"rows": [ROW1], "next": None}
    )

    result = run_source(
        conn, make_zeeker_client(), None, None, "sglawwatch/headlines", dry_run=True
    )

    assert [item.title for item in result.dry_run_items] == ["EP salary floor to rise"]
    assert result.processed == 0
    assert db.get_watermark(conn, "sglawwatch", "headlines") is None
    assert db.find_recent_cookies(conn, 1) == []
    assert db.list_registry(conn) == []


def test_run_source_rejects_judgment_table(conn):
    with pytest.raises(PipelineError, match="judgment pipeline"):
        run_source(conn, make_zeeker_client(), None, None, "zeeker-judgements/judgments")


def test_run_source_rejects_malformed_source(conn):
    with pytest.raises(PipelineError, match="<database>/<table>"):
        run_source(conn, make_zeeker_client(), None, None, "sglawwatch")


# ── CLI ──────────────────────────────────────────────────────────────


def test_cli_init_db(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("COOKIES_DB_PATH", str(db_path))

    assert main(["init-db"]) == 0

    assert db_path.exists()
    assert "initialised" in capsys.readouterr().out


@respx.mock
def test_cli_discover_surfaces_new_tables_inactive(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("COOKIES_DB_PATH", str(db_path))
    mock_zeeker_metadata()
    respx.get(f"{BASE_URL}/-/databases.json").respond(json=[{"name": "sglawwatch"}])
    respx.get(f"{BASE_URL}/sglawwatch.json").respond(
        json={"tables": [{"name": "headlines", "hidden": False, "count": 5}]}
    )

    assert main(["discover"]) == 0

    out = capsys.readouterr().out
    assert "NEW: sglawwatch/headlines" in out
    connection = db.init_db(db_path)
    try:
        (entry,) = db.list_registry(connection)
        assert (entry.zeeker_db, entry.table) == ("sglawwatch", "headlines")
        assert entry.pipeline == "news"
        assert entry.active is False
    finally:
        connection.close()


@respx.mock
def test_cli_run_dry_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COOKIES_DB_PATH", str(tmp_path / "cli.db"))
    mock_zeeker_metadata()
    respx.get(f"{BASE_URL}/sglawwatch/headlines.json").respond(
        json={"rows": [ROW1], "next": None}
    )

    assert main(["run", "--source", "sglawwatch/headlines", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "would process: EP salary floor to rise" in out
    assert "1 items pending" in out


def test_cli_run_requires_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COOKIES_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    assert main(["run", "--source", "sglawwatch/headlines"]) == 1

    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_cli_stats_empty_day(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COOKIES_DB_PATH", str(tmp_path / "cli.db"))

    assert main(["stats", "--date", "2026-06-11"]) == 0

    stats = json.loads(capsys.readouterr().out)
    assert stats["date"] == "2026-06-11"
    assert stats["total_cookies"] == 0
