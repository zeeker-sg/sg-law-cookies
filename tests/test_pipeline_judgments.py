"""run_source judgment routing + registry activation gate (PRD phase 3).

Zeeker HTTP is mocked with respx; all LLM backends are stubbed
(FakeBackend keyed by tool_name). No live LLM calls.
"""

import copy

import httpx
import pytest
import respx

from sg_law_cookies import db, folio
from sg_law_cookies.cli import main
from sg_law_cookies.folio import FOLIO_API_BASE
from sg_law_cookies.models import SourceRegistryEntry
from sg_law_cookies.pipeline import PipelineError, run_source
from sg_law_cookies.zeeker import BASE_URL, ZeekerClient

SOURCE = "zeeker-judgements/judgments"

METADATA = {
    "license": "CC-BY-4.0",
    "databases": {"zeeker-judgements": {"license": "Open"}, "*": {}},
}

JUDGMENT_ROW = {
    "id": "j1",
    "citation": "[2026] SGHC 34",
    "case_name": "Alpha Pte Ltd v Beta Pte Ltd",
    "court": "High Court",
    "source_url": "https://www.elitigation.sg/gd/s/2026_SGHC_34",
    "content_text": "1 " + "The plaintiff sued on a liquidated damages clause. " * 40,
    "court_summary": "Penalty clause upheld.",
    "subject_tags": '["Contract"]',
    "has_content": 1,
    "decision_date": "2026-06-01",
    "created_at": "2026-06-02T08:00:00.000001",
}

TEXTLESS_ROW = {
    "id": "j2",
    "citation": "[2026] SGHC 35",
    "case_name": "Gamma v Delta",
    "court": "High Court",
    "source_url": "https://www.elitigation.sg/gd/s/2026_SGHC_35",
    "content_text": "",
    "court_summary": "",
    "summary": "",
    "has_content": 0,
    "decision_date": "2026-06-02",
    "created_at": "2026-06-03T08:00:00.000002",
}

SHORT_STRUCTURE = {
    "citation": "[2026] SGHC 34",
    "court_name": "High Court of Singapore",
    "judges": ["Tan J"],
    "parties": ["Alpha Pte Ltd", "Beta Pte Ltd"],
    "background": "A dispute over liquidated damages under a supply agreement.",
    "issues": [
        {
            "question": "Whether the liquidated damages clause was an unenforceable penalty?",
            "holding": "The clause was not a penalty.",
            "reasoning": "It protected a legitimate commercial interest.",
            "raw_concepts": ["penalty doctrine"],
            "para_refs": ["1"],
        }
    ],
    "legislation": [],
    "cases_cited": [{"citation": "[2020] SGCA 1", "treatment": "followed"}],
    "orders": "Claim allowed with costs.",
}

TOPIC = {
    "headline": "High Court holds the clause was not a penalty",
    "summary": "The court held the liquidated damages clause enforceable, applying the legitimate-interest test.",
    "why_it_matters": "Liquidated damages clauses protecting a legitimate interest remain enforceable.",
    "significance": "medium",
    "raw_areas": ["Contract Law"],
    "raw_entities": [],
    "raw_concepts": [],
}


class FakeBackend:
    """Stubbed LLMBackend: canned dicts keyed by tool_name."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[str] = []

    def extract_topics(self, raw_item):  # pragma: no cover - guard
        raise AssertionError("judgment routing must never call extract_topics")

    def structured(self, system, user, schema, tool_name, max_tokens=16000, num_ctx=None):
        self.calls.append(tool_name)
        return copy.deepcopy(self.responses[tool_name])


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


def make_fake() -> FakeBackend:
    return FakeBackend(
        {"record_judgment": SHORT_STRUCTURE, "record_topics": {"topics": [TOPIC]}}
    )


def registry_entry(active: bool) -> SourceRegistryEntry:
    return SourceRegistryEntry(
        zeeker_db="zeeker-judgements",
        table="judgments",
        pipeline="judgment",
        license="Open",
        active=active,
    )


def mock_folio() -> None:
    respx.get(f"{FOLIO_API_BASE}/search/query").respond(
        json={
            "classes": [
                {
                    "iri": f"{FOLIO_API_BASE}/RContractLaw",
                    "label": "Contract Law",
                    "parent_class_of": [],
                }
            ]
        }
    )
    respx.get(f"{FOLIO_API_BASE}/search/label").respond(json={"results": []})


def mock_zeeker(rows: list[dict]) -> None:
    respx.get(f"{BASE_URL}/-/metadata.json").respond(json=METADATA)
    respx.get(f"{BASE_URL}/zeeker-judgements/judgments.json").respond(
        json={"rows": rows, "next": None}
    )


# ── routing ──────────────────────────────────────────────────────────


@respx.mock
def test_run_source_processes_judgment_end_to_end(conn):
    mock_folio()
    mock_zeeker([JUDGMENT_ROW])
    fake = make_fake()

    with httpx.Client() as folio_client:
        result = run_source(conn, make_zeeker_client(), fake, folio_client, SOURCE, limit=10)

    # short judgment: one structure call + one cookie call, no news path
    assert fake.calls == ["record_judgment", "record_topics"]
    assert result.processed == 1
    (cookie,) = result.cookies
    assert cookie.headline == "High Court holds the clause was not a penalty"

    stored = db.get_cookie(conn, cookie.id)
    assert stored is not None
    source = db.get_source(conn, stored.source_ids[0])
    assert source.item_type == "judgment"
    assert source.source_url == JUDGMENT_ROW["source_url"]

    meta = db.get_judgment_meta(conn, source.id)
    assert meta is not None
    assert meta.citation == "[2026] SGHC 34"
    assert meta.issues[0].holding == "The clause was not a penalty."
    assert meta.cases_cited[0].treatment == "followed"

    # watermark advanced on created_at; registry auto-added as judgment
    assert db.get_watermark(conn, "zeeker-judgements", "judgments") == JUDGMENT_ROW["created_at"]
    (entry,) = db.list_registry(conn)
    assert (entry.zeeker_db, entry.table, entry.pipeline, entry.active) == (
        "zeeker-judgements",
        "judgments",
        "judgment",
        True,
    )


@respx.mock
def test_run_source_skips_textless_judgment_rows_and_advances_watermark(conn):
    mock_folio()
    mock_zeeker([JUDGMENT_ROW, TEXTLESS_ROW])
    fake = make_fake()

    with httpx.Client() as folio_client:
        result = run_source(conn, make_zeeker_client(), fake, folio_client, SOURCE, limit=10)

    assert result.processed == 1
    assert result.skipped == 1
    # watermark moves past the skipped row so it is not refetched forever
    assert db.get_watermark(conn, "zeeker-judgements", "judgments") == TEXTLESS_ROW["created_at"]


# ── registry activation gate ─────────────────────────────────────────


def test_run_source_refuses_inactive_source(conn):
    db.upsert_registry_entry(conn, registry_entry(active=False))

    with pytest.raises(PipelineError, match=r"cookies activate"):
        run_source(conn, make_zeeker_client(), None, None, SOURCE, dry_run=True)


@respx.mock
def test_run_source_active_registry_entry_runs(conn):
    mock_folio()
    mock_zeeker([JUDGMENT_ROW])
    db.upsert_registry_entry(conn, registry_entry(active=True))
    fake = make_fake()

    with httpx.Client() as folio_client:
        result = run_source(conn, make_zeeker_client(), fake, folio_client, SOURCE, limit=10)

    assert result.processed == 1
    (entry,) = db.list_registry(conn)  # not duplicated by the auto-add
    assert entry.active is True


# ── dry run ──────────────────────────────────────────────────────────


@respx.mock
def test_run_source_dry_run_lists_without_llm_or_writes(conn):
    mock_zeeker([JUDGMENT_ROW])
    fake = make_fake()

    result = run_source(conn, make_zeeker_client(), fake, None, SOURCE, dry_run=True)

    assert fake.calls == []
    assert [item.title for item in result.dry_run_items] == ["Alpha Pte Ltd v Beta Pte Ltd"]
    assert result.processed == 0
    assert db.get_watermark(conn, "zeeker-judgements", "judgments") is None
    assert db.find_recent_cookies(conn, 1) == []
    assert db.list_registry(conn) == []


# ── CLI activate / deactivate ────────────────────────────────────────


def test_cli_activate_and_deactivate(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("COOKIES_DB_PATH", str(db_path))
    connection = db.init_db(db_path)
    db.upsert_registry_entry(connection, registry_entry(active=False))
    connection.close()

    assert main(["activate", SOURCE]) == 0
    out = capsys.readouterr().out
    assert "active" in out
    assert "pipeline: judgment" in out
    assert "license: Open" in out

    connection = db.init_db(db_path)
    (entry,) = db.list_registry(connection)
    assert entry.active is True
    connection.close()

    assert main(["deactivate", SOURCE]) == 0
    assert "inactive" in capsys.readouterr().out
    connection = db.init_db(db_path)
    (entry,) = db.list_registry(connection)
    assert entry.active is False
    connection.close()


def test_cli_activate_unknown_source_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COOKIES_DB_PATH", str(tmp_path / "cli.db"))

    assert main(["activate", "nope/nothing"]) == 1
    assert "cookies discover" in capsys.readouterr().err

    assert main(["activate", "malformed"]) == 1
    assert "<database>/<table>" in capsys.readouterr().err


def test_cli_run_inactive_source_prints_activate_hint(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "cli.db"
    monkeypatch.setenv("COOKIES_DB_PATH", str(db_path))
    connection = db.init_db(db_path)
    db.upsert_registry_entry(connection, registry_entry(active=False))
    connection.close()

    assert main(["run", "--source", SOURCE, "--dry-run"]) == 1
    assert f"cookies activate {SOURCE}" in capsys.readouterr().err
