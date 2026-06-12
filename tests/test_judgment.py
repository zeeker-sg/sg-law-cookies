"""Tests for the judgment pipeline (PRD 4.2, pseudocode section 3).

All LLM backends are stubbed (FakeBackend returns canned dicts keyed by
tool name); FOLIO HTTP calls are mocked with respx. No live LLM calls.
"""

import copy
from datetime import date

import httpx
import pytest
import respx

from sg_law_cookies import db, folio
from sg_law_cookies.folio import FOLIO_API_BASE
from sg_law_cookies.judgment import (
    CHUNK_NUM_CTX,
    MAX_ISSUES_SUMMARISED,
    extract_relevant_paragraphs,
    merge_structures,
    process_judgment,
    split_with_overlap,
    triage,
    _parse_structure,
)
from sg_law_cookies.models import JudgmentMeta, RawItem, Source
from sg_law_cookies.zeeker import BASE_URL

# ── fixtures and helpers ─────────────────────────────────────────────


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


class FakeBackend:
    """Stubbed LLMBackend: canned dicts keyed by tool_name.

    A dict value is returned (copied) on every call; a list value is a
    FIFO queue of per-call responses.
    """

    def __init__(self, responses: dict):
        self.responses = responses
        self.calls: list[dict] = []

    def extract_topics(self, raw_item):  # pragma: no cover - guard
        raise AssertionError("judgment pipeline must never call extract_topics")

    def structured(self, system, user, schema, tool_name, max_tokens=16000, num_ctx=None):
        self.calls.append(
            {
                "tool_name": tool_name,
                "system": system,
                "user": user,
                "schema": schema,
                "num_ctx": num_ctx,
            }
        )
        resp = self.responses[tool_name]
        if isinstance(resp, list):
            return copy.deepcopy(resp.pop(0))
        return copy.deepcopy(resp)

    def tools_called(self) -> list[str]:
        return [c["tool_name"] for c in self.calls]


def numbered_text(n_paras: int, style: str = "plain", para_chars: int = 450) -> str:
    """Judgment-like text with line-start paragraph numbers."""
    lines = []
    for n in range(1, n_paras + 1):
        prefix = f"[{n}] " if style == "bracket" else f"{n} "
        body = f"PARA{n} " + "the court considered the parties' submissions " * 20
        lines.append(prefix + body[:para_chars])
    return "\n".join(lines)


def make_item(text: str, url: str = "https://www.elitigation.sg/gd/s/2026_SGHC_34", **overrides) -> RawItem:
    fields = {
        "source_url": url,
        "zeeker_url": f"{BASE_URL}/zeeker-judgements/judgments/j1",
        "title": "Alpha Pte Ltd v Beta Pte Ltd",
        "raw_text": text,
        "date": date(2026, 6, 1),
        "source_id": "zeeker-judgements",
        "item_type": "judgment",
        "license": "CC-BY-4.0",
    }
    fields.update(overrides)
    return RawItem(**fields)


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


def topic_dict(headline: str = "High Court holds in [2026] SGHC 34 that the clause was not a penalty") -> dict:
    return {
        "headline": headline,
        "summary": "The court asked whether the clause was a penalty and held it was not, applying the legitimate-interest test.",
        "why_it_matters": "Liquidated damages clauses protecting a legitimate interest remain enforceable.",
        "significance": "medium",
        "raw_areas": ["Contract Law"],
        "raw_entities": ["High Court of Singapore"],
        "raw_concepts": ["penalty doctrine"],
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
            "reasoning": "It protected a legitimate commercial interest and was not extravagant.",
            "raw_concepts": ["penalty doctrine"],
            "para_refs": ["12-20"],
        }
    ],
    "legislation": ["Unfair Contract Terms Act 1977"],
    "cases_cited": [{"citation": "[2020] SGCA 1", "treatment": "followed"}],
    "orders": "Claim allowed with costs.",
}


# ── triage / chunking units ──────────────────────────────────────────


def test_triage_thresholds():
    assert triage(4_999) == "short"
    assert triage(5_000) == "medium"
    assert triage(19_999) == "medium"
    assert triage(20_000) == "long"


def test_split_with_overlap_covers_text_with_overlap():
    text = "x" * 90_000
    chunks = split_with_overlap(text)
    assert len(chunks) == 2
    assert chunks[0] == text[:60_000]
    assert chunks[1] == text[56_000:]  # 4k-char overlap
    assert split_with_overlap("short text") == ["short text"]


# ── paragraph extraction ─────────────────────────────────────────────


def test_extract_relevant_paragraphs_bracket_numbering():
    text = numbered_text(10, style="bracket", para_chars=80)
    result = extract_relevant_paragraphs(text, ["2-3"])
    # slices from para 2 to last+2 (para 5), excluding 1 and 6+
    assert "PARA2" in result and "PARA5" in result
    assert "PARA1 " not in result
    assert "PARA6" not in result


def test_extract_relevant_paragraphs_plain_numbering():
    text = numbered_text(10, style="plain", para_chars=80)
    result = extract_relevant_paragraphs(text, ["4"])
    assert "PARA4" in result and "PARA6" in result
    assert "PARA3 " not in result
    assert "PARA7" not in result


def test_extract_relevant_paragraphs_falls_back_to_full_text():
    text = numbered_text(5, para_chars=80)
    assert extract_relevant_paragraphs(text, []) == text  # no refs
    assert extract_relevant_paragraphs(text, ["999"]) == text  # unmatchable


def test_extract_relevant_paragraphs_falls_back_to_keyword_chunk():
    # > 20k estimated tokens, no usable paragraph numbering
    text = ("lorem ipsum dolor sit amet\n" * 3_300) + (
        "the negligence claim and the duty of care owed were discussed here"
    )
    question = "Whether the defendant owed a duty of care in negligence?"
    result = extract_relevant_paragraphs(text, [], question=question)
    assert "negligence" in result
    assert len(result) < len(text)  # a chunk, not the whole document


# ── merge_structures ─────────────────────────────────────────────────


def test_merge_structures_dedupes_and_reconciles():
    part1 = _parse_structure(
        {
            "citation": "[2026] SGCA 7",
            "court_name": "Court of Appeal of Singapore",
            "judges": ["Menon CJ"],
            "parties": ["Gamma", "Delta"],
            "background": "Part one of the facts.",
            "issues": [
                {
                    "question": "Whether the defendant owed a duty of care to the claimant",
                    "para_refs": ["10-20"],
                }
            ],
            "legislation": ["Civil Law Act 1909"],
            "cases_cited": [{"citation": "[2019] SGCA 5", "treatment": "referred"}],
            "orders": "",
        }
    )
    part2 = _parse_structure(
        {
            "citation": "",
            "court_name": "",
            "judges": [],
            "parties": [],
            "background": "Part two of the facts.",
            "issues": [
                {
                    "question": "Whether the defendant owed any duty of care to the claimant in negligence",
                    "para_refs": ["18-30"],
                },
                {
                    "question": "What is the appropriate measure of damages?",
                    "para_refs": ["120-150"],
                },
            ],
            "legislation": ["civil law act 1909", "Rules of Court 2021"],
            "cases_cited": [{"citation": "[2019] SGCA 5", "treatment": "followed"}],
            "orders": "Appeal dismissed.",
        }
    )

    merged = merge_structures([part1, part2])

    assert merged.citation == "[2026] SGCA 7"  # first non-empty wins
    assert merged.orders == "Appeal dismissed."
    assert len(merged.issues) == 2  # duty-of-care issue deduped by similarity
    assert merged.issues[0].para_refs == ["10-20", "18-30"]  # refs unioned
    assert merged.legislation == ["Civil Law Act 1909", "Rules of Court 2021"]
    assert merged.cases_cited == [{"citation": "[2019] SGCA 5", "treatment": "followed"}]
    assert "Part one" in merged.background and "Part two" in merged.background


# ── short path ───────────────────────────────────────────────────────


@respx.mock
def test_short_path_single_pass_stores_cookie_and_meta(conn):
    mock_folio()
    fake = FakeBackend(
        {
            "record_judgment": SHORT_STRUCTURE,
            "record_topics": {"topics": [topic_dict()]},
        }
    )
    item = make_item(
        numbered_text(20, para_chars=200),
        extras={"court_summary": "Penalty clause upheld.", "subject_tags": '["Contract"]'},
    )

    with httpx.Client() as folio_client:
        result = process_judgment(item, conn, fake, folio_client)

    # exactly two calls: single-pass extraction + cookie generation
    assert fake.tools_called() == ["record_judgment", "record_topics"]
    # catalogue hints prepended, clearly marked, before the judgment text
    first_user = fake.calls[0]["user"]
    assert first_user.startswith("CATALOGUE HINTS")
    assert "Penalty clause upheld." in first_user
    assert first_user.index("CATALOGUE HINTS") < first_user.index("PARA1")
    # cookie digest call receives structured data, not the raw text
    assert "PARA1" not in fake.calls[1]["user"]
    assert "[2026] SGHC 34" in fake.calls[1]["user"]

    assert len(result) == 1
    cookie = result[0]
    assert cookie.headline.startswith("High Court holds")
    assert cookie.folio_areas[0].preferred_label == "Contract Law"
    assert cookie.folio_entities[0].branch == "sg_local"
    stored_cookie = db.get_cookie(conn, cookie.id)
    assert stored_cookie is not None
    source = db.get_source(conn, stored_cookie.source_ids[0])
    assert source.item_type == "judgment"
    assert source.source_url == item.source_url

    meta = db.get_judgment_meta(conn, source.id)
    assert meta is not None
    assert meta.citation == "[2026] SGHC 34"
    assert meta.court.branch == "sg_local"  # resolved via local SG table
    assert meta.issues[0].holding == "The clause was not a penalty."
    assert meta.legislation[0].branch == "unresolved"  # not in FOLIO, kept as placeholder
    assert meta.cases_cited[0].treatment == "followed"
    assert meta.cases_cited[0].internal_ref is None  # not in our DB
    assert result.meta is not None and result.warnings == []

    unresolved = [t for t, _, _ in db.list_unresolved_terms(conn)]
    assert "penalty doctrine" in unresolved
    assert "Unfair Contract Terms Act 1977" in unresolved


# ── medium path ──────────────────────────────────────────────────────


@respx.mock
def test_medium_path_two_pass_with_per_issue_calls(conn):
    mock_folio()
    structure = {
        "citation": "[2026] SGHC 99",
        "court_name": "High Court of Singapore",
        "judges": ["Lee J"],
        "parties": ["Epsilon", "Zeta"],
        "background": "A negligence claim.",
        "issues": [
            {"question": "Whether a duty of care arose?", "para_refs": ["5-8"]},
            {"question": "What damages are recoverable?", "para_refs": ["40-42"]},
        ],
        "legislation": [],
        "cases_cited": [],
        "orders": "Judgment for the plaintiff.",
    }
    summaries = [
        {
            "holding": "A duty of care arose.",
            "reasoning": "Proximity and foreseeability were established.",
            "raw_concepts": ["duty of care"],
        },
        {
            "holding": "Only direct losses are recoverable.",
            "reasoning": "Remoteness barred the consequential heads.",
            "raw_concepts": ["remoteness of damage"],
        },
    ]
    fake = FakeBackend(
        {
            "record_structure": structure,
            "record_issue_summary": summaries,
            "record_topics": {"topics": [topic_dict("HC clarifies duty of care in [2026] SGHC 99")]},
        }
    )
    text = numbered_text(60)  # ~27k chars -> medium
    item = make_item(text, url="https://www.elitigation.sg/gd/s/2026_SGHC_99")

    with httpx.Client() as folio_client:
        result = process_judgment(item, conn, fake, folio_client)

    assert fake.tools_called() == [
        "record_structure",
        "record_issue_summary",
        "record_issue_summary",
        "record_topics",
    ]
    # bigger context window for medium/long calls
    assert fake.calls[0]["num_ctx"] == CHUNK_NUM_CTX
    assert fake.calls[1]["num_ctx"] == CHUNK_NUM_CTX
    # the first issue-summary call gets the issue question + paras 5..10 only
    issue_user = fake.calls[1]["user"]
    assert "Whether a duty of care arose?" in issue_user
    assert "PARA5" in issue_user and "PARA10" in issue_user
    assert "PARA11" not in issue_user and "PARA40" not in issue_user

    assert len(result) == 1
    meta = db.get_judgment_meta(conn, result[0].source_ids[0])
    assert meta.issues[0].holding == "A duty of care arose."
    assert meta.issues[1].holding == "Only direct losses are recoverable."
    assert meta.issues[1].folio_concepts[0].preferred_label == "remoteness of damage"
    # digest passed holdings (not raw text) to the cookie call
    assert "A duty of care arose." in fake.calls[3]["user"]


# ── long path ────────────────────────────────────────────────────────


@respx.mock
def test_long_path_chunked_merge_dedupes_and_cross_links(conn):
    mock_folio()

    # a prior judgment already in the DB, for citation cross-linking
    prior = Source(
        source_url="https://www.elitigation.sg/gd/s/2019_SGCA_5",
        zeeker_url=f"{BASE_URL}/zeeker-judgements/judgments/j0",
        title="Old v Older",
        raw_text="old text",
        date=date(2019, 1, 1),
        source_id="zeeker-judgements",
        item_type="judgment",
        license="CC-BY-4.0",
        token_count=2,
    )
    db.upsert_source(conn, prior)
    db.save_judgment_meta(
        conn, prior.id, JudgmentMeta(source_id=prior.id, citation="[2019] SGCA 5")
    )

    chunk_responses = [
        {
            "citation": "[2026] SGCA 7",
            "court_name": "Court of Appeal of Singapore",
            "judges": ["Menon CJ"],
            "parties": ["Gamma", "Delta"],
            "background": "The appellant sued in negligence.",
            "issues": [
                {
                    "question": "Whether the defendant owed a duty of care to the claimant",
                    "para_refs": ["10-20"],
                }
            ],
            "legislation": ["Civil Law Act 1909"],
            "cases_cited": [{"citation": "[2019] SGCA 5", "treatment": "referred"}],
            "orders": "",
        },
        {
            "citation": "",
            "court_name": "",
            "judges": [],
            "parties": [],
            "background": "",
            "issues": [
                {
                    # duplicate of the chunk-1 issue, differently worded
                    "question": "Whether the defendant owed any duty of care to the claimant in negligence",
                    "para_refs": ["18-30"],
                },
                {"question": "What is the appropriate measure of damages?", "para_refs": ["150-160"]},
            ],
            "legislation": ["Rules of Court 2021"],
            "cases_cited": [{"citation": "[2019] SGCA 5", "treatment": "followed"}],
            "orders": "Appeal dismissed with costs.",
        },
    ]
    summaries = [
        {"holding": "A duty arose.", "reasoning": "Proximity established.", "raw_concepts": ["duty of care"]},
        {"holding": "Full measure awarded.", "reasoning": "Standard principles.", "raw_concepts": ["damages"]},
    ]
    fake = FakeBackend(
        {
            "record_chunk_structure": chunk_responses,
            "record_issue_summary": summaries,
            "record_topics": {"topics": [topic_dict("CA affirms duty of care in [2026] SGCA 7")]},
        }
    )
    text = numbered_text(200)  # ~90k chars -> long, 2 chunks
    item = make_item(text, url="https://www.elitigation.sg/gd/s/2026_SGCA_7")

    with httpx.Client() as folio_client:
        result = process_judgment(item, conn, fake, folio_client)

    assert fake.tools_called() == [
        "record_chunk_structure",
        "record_chunk_structure",
        "record_issue_summary",
        "record_issue_summary",
        "record_topics",
    ]
    assert fake.calls[0]["num_ctx"] == CHUNK_NUM_CTX
    assert fake.calls[1]["num_ctx"] == CHUNK_NUM_CTX

    meta = db.get_judgment_meta(conn, result[0].source_ids[0])
    assert meta.citation == "[2026] SGCA 7"
    assert meta.orders == "Appeal dismissed with costs."
    assert len(meta.issues) == 2  # duplicated issue merged
    assert meta.issues[0].holding == "A duty arose."
    assert [ref.preferred_label for ref in meta.legislation] == [
        "Civil Law Act 1909",
        "Rules of Court 2021",
    ]
    # one deduped citation, treatment upgraded from 'referred', cross-linked
    assert len(meta.cases_cited) == 1
    assert meta.cases_cited[0].treatment == "followed"
    assert meta.cases_cited[0].internal_ref == prior.id


# ── idempotency ──────────────────────────────────────────────────────


@respx.mock
def test_idempotent_rerun_skips_llm_and_returns_existing(conn):
    mock_folio()
    fake = FakeBackend(
        {"record_judgment": SHORT_STRUCTURE, "record_topics": {"topics": [topic_dict()]}}
    )
    item = make_item(numbered_text(20, para_chars=200))

    with httpx.Client() as folio_client:
        first = process_judgment(item, conn, fake, folio_client)
        calls_after_first = len(fake.calls)
        second = process_judgment(item, conn, fake, folio_client)

    assert len(fake.calls) == calls_after_first  # no further LLM calls
    assert [c.id for c in second] == [c.id for c in first]
    assert second.meta is not None
    assert second.meta.citation == "[2026] SGHC 34"
    assert len(db.find_recent_cookies(conn, 1)) == 1  # no duplicate cookies


# ── issue cap ────────────────────────────────────────────────────────


@respx.mock
def test_issues_capped_at_eight_with_warning(conn):
    mock_folio()
    structure = {
        "citation": "[2026] SGHC 120",
        "court_name": "High Court of Singapore",
        "judges": [],
        "parties": [],
        "background": "",
        "issues": [
            {"question": f"Question number {n} about topic {n}?", "para_refs": []}
            for n in range(1, 11)  # 10 issues
        ],
        "legislation": [],
        "cases_cited": [],
        "orders": "",
    }
    fake = FakeBackend(
        {
            "record_structure": structure,
            # dict, not list: same canned summary for every per-issue call
            "record_issue_summary": {
                "holding": "Decided.",
                "reasoning": "Reasoned.",
                "raw_concepts": [],
            },
            "record_topics": {"topics": []},
        }
    )
    item = make_item(numbered_text(60), url="https://www.elitigation.sg/gd/s/2026_SGHC_120")

    with httpx.Client() as folio_client:
        result = process_judgment(item, conn, fake, folio_client)

    summary_calls = [t for t in fake.tools_called() if t == "record_issue_summary"]
    assert len(summary_calls) == MAX_ISSUES_SUMMARISED
    assert len(result.warnings) == 1
    assert str(MAX_ISSUES_SUMMARISED) in result.warnings[0]
    assert "10 issues" in result.warnings[0]

    # no cookies (empty topics) but the meta is still stored against the source
    assert result == []
    stored = db.find_source_by_url(conn, item.source_url)
    meta = db.get_judgment_meta(conn, stored.id)
    assert len(meta.issues) == 10  # all issues kept in meta
    assert meta.issues[0].holding == "Decided."
    assert meta.issues[9].holding == ""  # beyond the cap: question only


@respx.mock
def test_catalogue_citation_and_court_override_llm(conn):
    # Regression: model stored case number "OC 1154/2025" while Zeeker's
    # extras carried the authoritative "[2026] SGDC 136"; bare "District
    # Court" resolved to a US court via FOLIO substring match.
    mock_folio()
    fake = FakeBackend(
        {
            "record_judgment": SHORT_STRUCTURE,
            "record_topics": {"topics": [topic_dict()]},
        }
    )
    item = make_item(
        numbered_text(20, para_chars=200),
        extras={"citation": "[2026] SGDC 136", "court": "District Court"},
    )

    with httpx.Client() as folio_client:
        process_judgment(item, conn, fake, folio_client)

    source = db.find_source_by_url(conn, item.source_url)
    meta = db.get_judgment_meta(conn, source.id)
    assert meta.citation == "[2026] SGDC 136"  # not SHORT_STRUCTURE's citation
    assert meta.court is not None
    assert meta.court.preferred_label == "State Courts"
    assert meta.court.branch == "sg_local"
