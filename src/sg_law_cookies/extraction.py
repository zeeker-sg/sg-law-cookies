"""LLM extraction for the news pipeline (PRD section 4.1 step 1).

A single Messages API call with a forced tool choice so the model must
return JSON conforming to the TopicExtraction shape. Only the raw_* fields
are produced here; folio_* fields are filled by the FOLIO resolution step.
"""

from typing import Any

import anthropic

from sg_law_cookies.models import RawItem, TopicExtraction
from sg_law_cookies.prompts import NEWS_SYSTEM_PROMPT

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 16000

NEWS_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "record_topics",
    "description": (
        "Record every distinct legal proposition extracted from the article. "
        "Pass an empty topics list if the article contains no legal signal."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "topics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "headline": {
                            "type": "string",
                            "description": "One crisp sentence stating the legal development.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "2-3 sentence summary of the substance.",
                        },
                        "why_it_matters": {
                            "type": "string",
                            "description": "One sentence on why a Singapore lawyer should care.",
                        },
                        "significance": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Significance level per the rubric.",
                        },
                        "raw_areas": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Free-text areas of law engaged.",
                        },
                        "raw_entities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Free-text entities involved (organisations, courts, statutes).",
                        },
                        "raw_concepts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Free-text legal doctrines, principles or concepts engaged.",
                        },
                    },
                    "required": [
                        "headline",
                        "summary",
                        "why_it_matters",
                        "significance",
                        "raw_areas",
                        "raw_entities",
                        "raw_concepts",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["topics"],
        "additionalProperties": False,
    },
}


class ExtractionError(RuntimeError):
    """Raised when the model response cannot be parsed into TopicExtraction objects."""


def format_news_user_prompt(raw_item: RawItem) -> str:
    return (
        f"Source URL: {raw_item.source_url}\n"
        f"Title: {raw_item.title}\n"
        f"Date: {raw_item.date.isoformat()}\n\n"
        f"Article text:\n{raw_item.raw_text}"
    )


def extract_news(
    client: anthropic.Anthropic,
    raw_item: RawItem,
    model: str = DEFAULT_MODEL,
) -> list[TopicExtraction]:
    """Run the single-pass news extraction call and parse the tool result."""
    response = client.messages.create(
        model=model,
        max_tokens=DEFAULT_MAX_TOKENS,
        system=NEWS_SYSTEM_PROMPT,
        tools=[NEWS_EXTRACTION_TOOL],
        tool_choice={"type": "tool", "name": NEWS_EXTRACTION_TOOL["name"]},
        messages=[{"role": "user", "content": format_news_user_prompt(raw_item)}],
    )

    tool_use = next(
        (
            block
            for block in response.content
            if getattr(block, "type", None) == "tool_use"
            and block.name == NEWS_EXTRACTION_TOOL["name"]
        ),
        None,
    )
    if tool_use is None:
        raise ExtractionError(
            f"No {NEWS_EXTRACTION_TOOL['name']!r} tool call in model response "
            f"(stop_reason={getattr(response, 'stop_reason', None)!r})"
        )

    topics = tool_use.input.get("topics")
    if not isinstance(topics, list):
        raise ExtractionError(
            f"Tool input missing 'topics' list: {tool_use.input!r}"
        )

    return [TopicExtraction.model_validate(topic) for topic in topics]
