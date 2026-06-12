"""Tests for the generic structured() call on both LLM backends.

All backends are stubbed: a fake Anthropic client for AnthropicBackend,
respx for OllamaBackend. No live LLM calls.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from sg_law_cookies.extraction import NEWS_EXTRACTION_TOOL, ExtractionError
from sg_law_cookies.llm import AnthropicBackend, OllamaBackend
from sg_law_cookies.prompts_judgment import (
    JUDGMENT_CHUNK_TOOL,
    JUDGMENT_COOKIE_PROMPT,
    JUDGMENT_COOKIE_TOOL,
    JUDGMENT_ISSUE_SUMMARY,
    JUDGMENT_ISSUE_SUMMARY_TOOL,
    JUDGMENT_SINGLE_PASS,
    JUDGMENT_SINGLE_PASS_TOOL,
    JUDGMENT_STRUCTURE,
    JUDGMENT_STRUCTURE_TOOL,
)

ISSUE_SUMMARY = {
    "holding": "The respondent breached its protection obligation under s 24 PDPA.",
    "reasoning": "The database was exposed without access controls for months. "
    "Reasonable security arrangements would have detected the misconfiguration.",
    "raw_concepts": ["protection obligation", "reasonable security arrangements"],
}

STRUCTURE = {
    "citation": "[2026] SGHC 34",
    "court_name": "General Division of the High Court",
    "judges": ["Tan J"],
    "parties": ["Alpha Pte Ltd", "Beta Pte Ltd"],
    "background": "A dispute over a share sale agreement.",
    "issues": [{"question": "Was the clause a penalty?", "para_refs": ["45-78"]}],
    "legislation": ["Contracts (Rights of Third Parties) Act 2001"],
    "cases_cited": [{"citation": "[2020] SGCA 1", "treatment": "followed"}],
    "orders": "Claim allowed with costs.",
}


# ── Anthropic backend ─────────────────────────────────────────────────


class StubMessages:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


class StubClient:
    def __init__(self, response):
        self.messages = StubMessages(response)


def tool_use_block(name, input):
    return SimpleNamespace(type="tool_use", id="toolu_01", name=name, input=input)


def make_response(*blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_anthropic_structured_returns_tool_input():
    client = StubClient(
        make_response(tool_use_block("record_issue_summary", ISSUE_SUMMARY))
    )
    backend = AnthropicBackend(client, model="claude-sonnet-4-6")

    result = backend.structured(
        system=JUDGMENT_ISSUE_SUMMARY,
        user="Issue: ...\n\nRelevant text: ...",
        schema=JUDGMENT_ISSUE_SUMMARY_TOOL["input_schema"],
        tool_name=JUDGMENT_ISSUE_SUMMARY_TOOL["name"],
    )

    assert result == ISSUE_SUMMARY


def test_anthropic_structured_request_shape():
    client = StubClient(make_response(tool_use_block("record_structure", STRUCTURE)))
    backend = AnthropicBackend(client, model="claude-sonnet-4-6")

    backend.structured(
        system=JUDGMENT_STRUCTURE,
        user="Full judgment text here.",
        schema=JUDGMENT_STRUCTURE_TOOL["input_schema"],
        tool_name="record_structure",
        max_tokens=8000,
        num_ctx=32768,  # Ollama-only knob, must be accepted and ignored
    )

    (call,) = client.messages.calls
    assert call["model"] == "claude-sonnet-4-6"
    assert call["max_tokens"] == 8000
    assert call["system"] == JUDGMENT_STRUCTURE
    assert call["tools"] == [
        {
            "name": "record_structure",
            "input_schema": JUDGMENT_STRUCTURE_TOOL["input_schema"],
        }
    ]
    assert call["tool_choice"] == {"type": "tool", "name": "record_structure"}
    assert call["messages"] == [
        {"role": "user", "content": "Full judgment text here."}
    ]
    assert "num_ctx" not in call


def test_anthropic_structured_no_tool_call_raises():
    text_only = SimpleNamespace(type="text", text="Cannot comply.")
    client = StubClient(make_response(text_only, stop_reason="end_turn"))
    backend = AnthropicBackend(client)

    with pytest.raises(ExtractionError, match="record_judgment"):
        backend.structured(
            system=JUDGMENT_SINGLE_PASS,
            user="text",
            schema=JUDGMENT_SINGLE_PASS_TOOL["input_schema"],
            tool_name="record_judgment",
        )


def test_anthropic_structured_wrong_tool_name_raises():
    client = StubClient(make_response(tool_use_block("some_other_tool", {})))
    backend = AnthropicBackend(client)

    with pytest.raises(ExtractionError):
        backend.structured(
            system=JUDGMENT_STRUCTURE,
            user="text",
            schema=JUDGMENT_STRUCTURE_TOOL["input_schema"],
            tool_name="record_structure",
        )


def test_anthropic_structured_non_dict_input_raises():
    client = StubClient(
        make_response(tool_use_block("record_issue_summary", ["not", "a", "dict"]))
    )
    backend = AnthropicBackend(client)

    with pytest.raises(ExtractionError, match="not an object"):
        backend.structured(
            system=JUDGMENT_ISSUE_SUMMARY,
            user="text",
            schema=JUDGMENT_ISSUE_SUMMARY_TOOL["input_schema"],
            tool_name="record_issue_summary",
        )


# ── Ollama backend ────────────────────────────────────────────────────


def _ollama_response(payload) -> dict:
    return {"message": {"role": "assistant", "content": json.dumps(payload)}}


@respx.mock
def test_ollama_structured_returns_parsed_dict():
    route = respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json=_ollama_response(STRUCTURE))
    )
    backend = OllamaBackend(model="qwen3:8b", client=httpx.Client())

    result = backend.structured(
        system=JUDGMENT_STRUCTURE,
        user="Full judgment text here.",
        schema=JUDGMENT_STRUCTURE_TOOL["input_schema"],
        tool_name="record_structure",
    )

    assert result == STRUCTURE
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen3:8b"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["format"] == JUDGMENT_STRUCTURE_TOOL["input_schema"]
    assert body["messages"][0] == {"role": "system", "content": JUDGMENT_STRUCTURE}
    assert body["messages"][1] == {
        "role": "user",
        "content": "Full judgment text here.",
    }
    assert body["options"] == {"temperature": 0, "num_ctx": 16384}  # default


@respx.mock
def test_ollama_structured_num_ctx_override_for_long_chunks():
    route = respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(
            200, json=_ollama_response({**STRUCTURE, "citation": ""})
        )
    )
    backend = OllamaBackend(client=httpx.Client())

    backend.structured(
        system="chunk prompt",
        user="long judgment chunk",
        schema=JUDGMENT_CHUNK_TOOL["input_schema"],
        tool_name="record_chunk_structure",
        num_ctx=32768,
    )

    body = json.loads(route.calls[0].request.content)
    assert body["options"] == {"temperature": 0, "num_ctx": 32768}


@respx.mock
def test_ollama_structured_think_none_omits_flag():
    route = respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json=_ollama_response(ISSUE_SUMMARY))
    )
    backend = OllamaBackend(client=httpx.Client(), think=None)

    backend.structured(
        system=JUDGMENT_ISSUE_SUMMARY,
        user="issue text",
        schema=JUDGMENT_ISSUE_SUMMARY_TOOL["input_schema"],
        tool_name="record_issue_summary",
    )

    body = json.loads(route.calls[0].request.content)
    assert "think" not in body


@respx.mock
def test_ollama_structured_non_json_raises():
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "not json"}}
        )
    )
    backend = OllamaBackend(client=httpx.Client())

    with pytest.raises(ExtractionError, match="non-JSON"):
        backend.structured(
            system="s",
            user="u",
            schema=JUDGMENT_ISSUE_SUMMARY_TOOL["input_schema"],
            tool_name="record_issue_summary",
        )


@respx.mock
def test_ollama_structured_non_object_json_raises():
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json=_ollama_response([1, 2, 3]))
    )
    backend = OllamaBackend(client=httpx.Client())

    with pytest.raises(ExtractionError, match="non-object"):
        backend.structured(
            system="s",
            user="u",
            schema=JUDGMENT_ISSUE_SUMMARY_TOOL["input_schema"],
            tool_name="record_issue_summary",
        )


# ── Judgment prompt/schema sanity ─────────────────────────────────────


def test_cookie_tool_is_news_extraction_tool():
    # Pseudocode section 3 step 4: judgment cookies use the SAME schema as
    # news so both pipelines are interchangeable downstream.
    assert JUDGMENT_COOKIE_TOOL is NEWS_EXTRACTION_TOOL
    assert "structured" in JUDGMENT_COOKIE_PROMPT.lower()


def test_structure_tool_issues_are_questions_only():
    issue = JUDGMENT_STRUCTURE_TOOL["input_schema"]["properties"]["issues"]["items"]
    assert set(issue["properties"]) == {"question", "para_refs"}
    assert "holding" not in issue["properties"]


def test_single_pass_tool_issues_carry_summaries():
    issue = JUDGMENT_SINGLE_PASS_TOOL["input_schema"]["properties"]["issues"]["items"]
    assert {"question", "holding", "reasoning", "raw_concepts"} <= set(
        issue["properties"]
    )
    treatment = JUDGMENT_SINGLE_PASS_TOOL["input_schema"]["properties"]["cases_cited"][
        "items"
    ]["properties"]["treatment"]
    assert treatment["enum"] == ["followed", "distinguished", "overruled", "referred"]


def test_chunk_tool_same_shape_as_structure_tool():
    assert set(JUDGMENT_CHUNK_TOOL["input_schema"]["properties"]) == set(
        JUDGMENT_STRUCTURE_TOOL["input_schema"]["properties"]
    )
