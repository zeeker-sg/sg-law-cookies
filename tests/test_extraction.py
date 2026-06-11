from datetime import date
from types import SimpleNamespace

import pytest

from sg_law_cookies.extraction import (
    DEFAULT_MODEL,
    NEWS_EXTRACTION_TOOL,
    ExtractionError,
    extract_news,
)
from sg_law_cookies.models import RawItem, TopicExtraction
from sg_law_cookies.prompts import NEWS_SYSTEM_PROMPT


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


def tool_use_block(topics):
    return SimpleNamespace(
        type="tool_use",
        id="toolu_01",
        name=NEWS_EXTRACTION_TOOL["name"],
        input={"topics": topics},
    )


def make_response(*blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def make_raw_item() -> RawItem:
    return RawItem(
        source_url="https://www.singaporelawwatch.sg/Headlines/ep-salary-floor",
        zeeker_url="https://data.zeeker.sg/sglawwatch/headlines/42",
        title="EP salary floor to rise",
        raw_text="MOM announced the EP qualifying salary will rise to SGD 6,000...",
        date=date(2026, 6, 10),
        source_id="sglawwatch",
        item_type="news",
        license="restricted",
    )


def topic_payload(headline="EP salary floor rising to SGD 6,000 from Jan 2027"):
    return {
        "headline": headline,
        "summary": "MOM will raise the Employment Pass qualifying salary. "
        "The change takes effect for new applications from January 2027.",
        "why_it_matters": "Employers must rebenchmark EP holders' salaries before renewal.",
        "significance": "high",
        "raw_areas": ["employment law", "immigration law"],
        "raw_entities": ["Ministry of Manpower"],
        "raw_concepts": ["work pass qualifying salary"],
    }


def test_single_topic_parsed():
    payload = topic_payload()
    client = StubClient(make_response(tool_use_block([payload])))

    topics = extract_news(client, make_raw_item())

    assert len(topics) == 1
    topic = topics[0]
    assert isinstance(topic, TopicExtraction)
    assert topic.headline == payload["headline"]
    assert topic.summary == payload["summary"]
    assert topic.why_it_matters == payload["why_it_matters"]
    assert topic.significance == "high"
    assert topic.raw_areas == ["employment law", "immigration law"]
    assert topic.raw_entities == ["Ministry of Manpower"]
    assert topic.raw_concepts == ["work pass qualifying salary"]
    # folio fields are filled later, by the FOLIO resolution step
    assert topic.folio_areas == []
    assert topic.folio_entities == []
    assert topic.folio_concepts == []
    assert topic.unresolved == []
    assert topic.is_duplicate is False


def test_multi_topic_split_preserved_in_order():
    payloads = [
        topic_payload("First proposition"),
        {**topic_payload("Second proposition"), "significance": "low"},
    ]
    client = StubClient(make_response(tool_use_block(payloads)))

    topics = extract_news(client, make_raw_item())

    assert [t.headline for t in topics] == ["First proposition", "Second proposition"]
    assert [t.significance for t in topics] == ["high", "low"]


def test_zero_topics_returns_empty_list():
    client = StubClient(make_response(tool_use_block([])))
    assert extract_news(client, make_raw_item()) == []


def test_no_tool_call_raises_extraction_error():
    text_only = SimpleNamespace(type="text", text="I cannot extract anything.")
    client = StubClient(make_response(text_only, stop_reason="end_turn"))

    with pytest.raises(ExtractionError, match="No 'record_topics' tool call"):
        extract_news(client, make_raw_item())


def test_request_shape_forces_tool_and_uses_default_model():
    client = StubClient(make_response(tool_use_block([])))
    raw_item = make_raw_item()

    extract_news(client, raw_item)

    (call,) = client.messages.calls
    assert call["model"] == DEFAULT_MODEL == "claude-sonnet-4-6"
    assert call["system"] == NEWS_SYSTEM_PROMPT
    assert call["tools"] == [NEWS_EXTRACTION_TOOL]
    assert call["tool_choice"] == {"type": "tool", "name": "record_topics"}
    assert len(call["messages"]) == 1
    assert call["messages"][0]["role"] == "user"
    user_content = call["messages"][0]["content"]
    assert raw_item.raw_text in user_content
    assert raw_item.source_url in user_content
    assert raw_item.title in user_content


def test_model_override():
    client = StubClient(make_response(tool_use_block([])))

    extract_news(client, make_raw_item(), model="claude-opus-4-8")

    (call,) = client.messages.calls
    assert call["model"] == "claude-opus-4-8"


def test_invalid_tool_input_raises_extraction_error():
    block = SimpleNamespace(
        type="tool_use",
        id="toolu_02",
        name=NEWS_EXTRACTION_TOOL["name"],
        input={"not_topics": []},
    )
    client = StubClient(make_response(block))

    with pytest.raises(ExtractionError, match="missing 'topics'"):
        extract_news(client, make_raw_item())
