import json
from datetime import date

import httpx
import pytest
import respx

from sg_law_cookies.extraction import ExtractionError
from sg_law_cookies.llm import OllamaBackend, as_backend
from sg_law_cookies.models import RawItem

RAW_ITEM = RawItem(
    source_url="https://www.singaporelawwatch.sg/Headlines/example",
    zeeker_url="https://data.zeeker.sg/sglawwatch/headlines/1",
    title="Example headline",
    raw_text="MOM announced the EP salary floor rises to $6,000 from Jan 2027.",
    date=date(2026, 6, 11),
    source_id="sglawwatch",
    item_type="news",
    license="CC-BY-4.0",
)

TOPIC = {
    "headline": "EP salary floor rises to $6,000 from January 2027",
    "summary": "MOM raised the Employment Pass qualifying salary. Employers must meet the new floor for renewals and new applications.",
    "why_it_matters": "Employers must re-benchmark EP holders before renewal.",
    "significance": "high",
    "raw_areas": ["Employment"],
    "raw_entities": ["Ministry of Manpower"],
    "raw_concepts": ["work passes"],
}


def _ollama_response(payload: dict) -> dict:
    return {"message": {"role": "assistant", "content": json.dumps(payload)}}


@respx.mock
def test_ollama_backend_parses_topics():
    route = respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json=_ollama_response({"topics": [TOPIC]}))
    )
    backend = OllamaBackend(model="qwen3:8b", client=httpx.Client())
    topics = backend.extract_topics(RAW_ITEM)
    assert len(topics) == 1
    assert topics[0].significance == "high"
    body = json.loads(route.calls[0].request.content)
    assert body["model"] == "qwen3:8b"
    assert body["format"]["properties"]["topics"]  # schema-constrained output
    assert body["stream"] is False
    assert body["think"] is False  # reasoning traces off by default


@respx.mock
def test_ollama_backend_zero_topics():
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(200, json=_ollama_response({"topics": []}))
    )
    backend = OllamaBackend(client=httpx.Client())
    assert backend.extract_topics(RAW_ITEM) == []


@respx.mock
def test_ollama_backend_rejects_non_json():
    respx.post("http://localhost:11434/api/chat").mock(
        return_value=httpx.Response(
            200, json={"message": {"role": "assistant", "content": "not json"}}
        )
    )
    backend = OllamaBackend(client=httpx.Client())
    with pytest.raises(ExtractionError):
        backend.extract_topics(RAW_ITEM)


def test_as_backend_passes_through_backends():
    backend = OllamaBackend(client=httpx.Client())
    assert as_backend(backend) is backend


def test_as_backend_wraps_anthropic_client():
    class StubClient:  # looks like anthropic.Anthropic (has .messages, no .extract_topics)
        messages = object()

    wrapped = as_backend(StubClient(), model="claude-sonnet-4-6")
    assert wrapped.model == "claude-sonnet-4-6"
    assert hasattr(wrapped, "extract_topics")
