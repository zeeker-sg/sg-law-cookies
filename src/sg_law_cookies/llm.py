"""Pluggable LLM backends for the extraction step.

Anthropic is the primary backend (tool use forces schema conformance).
Ollama is a local alternative for dev and cost-free runs: its /api/chat
endpoint accepts the same JSON schema in the `format` field, so both
backends produce identical TopicExtraction lists.
"""

import json
from typing import Protocol

import anthropic
import httpx

from sg_law_cookies.extraction import (
    DEFAULT_MODEL,
    NEWS_EXTRACTION_TOOL,
    ExtractionError,
    extract_news,
    format_news_user_prompt,
)
from sg_law_cookies.models import RawItem, TopicExtraction
from sg_law_cookies.prompts import NEWS_SYSTEM_PROMPT

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen3:8b"


DEFAULT_NUM_CTX = 16384


class LLMBackend(Protocol):
    def extract_topics(self, raw_item: RawItem) -> list[TopicExtraction]: ...

    def structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
        max_tokens: int = 16000,
        num_ctx: int | None = None,
    ) -> dict: ...


class AnthropicBackend:
    def __init__(self, client: anthropic.Anthropic, model: str = DEFAULT_MODEL):
        self.client = client
        self.model = model

    def extract_topics(self, raw_item: RawItem) -> list[TopicExtraction]:
        return extract_news(self.client, raw_item, model=self.model)

    def structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,
        max_tokens: int = 16000,
        num_ctx: int | None = None,  # noqa: ARG002 — Ollama-only knob, accepted for parity
    ) -> dict:
        """One schema-constrained call: forced tool use, returns the tool input dict."""
        tool = {"name": tool_name, "input_schema": schema}
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool_name},
            messages=[{"role": "user", "content": user}],
        )
        tool_use = next(
            (
                block
                for block in response.content
                if getattr(block, "type", None) == "tool_use" and block.name == tool_name
            ),
            None,
        )
        if tool_use is None:
            raise ExtractionError(
                f"No {tool_name!r} tool call in model response "
                f"(stop_reason={getattr(response, 'stop_reason', None)!r})"
            )
        if not isinstance(tool_use.input, dict):
            raise ExtractionError(f"Tool input is not an object: {tool_use.input!r}")
        return tool_use.input


class OllamaBackend:
    def __init__(
        self,
        model: str = DEFAULT_OLLAMA_MODEL,
        host: str = DEFAULT_OLLAMA_HOST,
        client: httpx.Client | None = None,
        think: bool | None = False,
    ):
        self.model = model
        self.host = host.rstrip("/")
        self.client = client or httpx.Client(timeout=600.0)
        # Reasoning models burn minutes on thinking traces at local token
        # speeds before emitting the constrained JSON — off by default.
        # None omits the flag for models that reject it.
        self.think = think

    def extract_topics(self, raw_item: RawItem) -> list[TopicExtraction]:
        data = self.structured(
            system=NEWS_SYSTEM_PROMPT,
            user=format_news_user_prompt(raw_item),
            schema=NEWS_EXTRACTION_TOOL["input_schema"],
            tool_name=NEWS_EXTRACTION_TOOL["name"],
        )
        # Some Ollama models return {"topics": [...]} as instructed,
        # others return the array directly. Accept both.
        topics = data.get("topics") if isinstance(data, dict) else data
        if not isinstance(topics, list):
            raise ExtractionError(
                f"Ollama response missing 'topics' list: got {type(data).__name__} {data!r}"
            )
        # Normalize: some models return comma-separated strings instead of lists
        normalized = []
        for topic in topics:
            for key in ("raw_areas", "raw_entities", "raw_concepts"):
                val = topic.get(key)
                if isinstance(val, str):
                    topic[key] = [v.strip() for v in val.split(",") if v.strip()]
            normalized.append(topic)
        return [TopicExtraction.model_validate(topic) for topic in normalized]

    def structured(
        self,
        system: str,
        user: str,
        schema: dict,
        tool_name: str,  # noqa: ARG002 — Anthropic-only knob, accepted for parity
        max_tokens: int = 16000,  # noqa: ARG002 — Anthropic-only knob, accepted for parity
        num_ctx: int | None = None,
    ) -> dict:
        """One LLM call, returns parsed JSON.

        Ollama's `format=schema` is unreliable with cloud-routed models and
        even some local models emit YAML or text. We drop the schema constraint
        from the payload and instead append a JSON-example instruction to the
        system prompt. If the response still isn't valid JSON, we attempt a
        repair call before giving up.
        """
        example = json.dumps(schema, indent=2)
        augmented_system = (
            f"{system}\n\n"
            f"Respond ONLY with a single JSON object matching this schema:\n"
            f"{example}\n\n"
            "Do not wrap in markdown code fences. Output raw JSON only."
        )
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": augmented_system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0, "num_ctx": num_ctx or DEFAULT_NUM_CTX},
        }
        if self.think is not None:
            payload["think"] = self.think
        response = self.client.post(f"{self.host}/api/chat", json=payload)
        response.raise_for_status()
        raw = response.json().get("message", {}).get("content", "")
        data = self._parse_json(raw, schema)
        if data is not None:
            return data

        # Repair attempt: ask the model to fix its malformed output
        repair_payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a JSON repair tool. Convert the user's text into valid JSON matching the requested schema. Output raw JSON only, no markdown fences.",
                },
                {
                    "role": "user",
                    "content": f"Schema:\n{example}\n\nText to convert:\n{raw[:4000]}",
                },
            ],
            "stream": False,
            "options": {"temperature": 0, "num_ctx": num_ctx or DEFAULT_NUM_CTX},
        }
        if self.think is not None:
            repair_payload["think"] = self.think
        repair_resp = self.client.post(f"{self.host}/api/chat", json=repair_payload)
        repair_resp.raise_for_status()
        repair_raw = repair_resp.json().get("message", {}).get("content", "")
        data = self._parse_json(repair_raw, schema)
        if data is not None:
            return data

        raise ExtractionError(
            f"Ollama returned non-JSON content even after repair: {raw[:200]!r}"
        )

    @staticmethod
    def _parse_json(raw: str, schema: dict) -> dict | None:
        """Strip fences and parse; return None on failure."""
        content = raw
        if content.startswith("```json"):
            content = content[7:]
        elif content.startswith("```"):
            content = content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
        if not content:
            if schema.get("properties", {}).get("topics", {}).get("type") == "array":
                return {"topics": []}
            return None
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None


def as_backend(llm: object, model: str = DEFAULT_MODEL) -> LLMBackend:
    """Accept a backend or a bare Anthropic client (legacy call sites)."""
    if hasattr(llm, "extract_topics"):
        return llm  # type: ignore[return-value]
    return AnthropicBackend(llm, model)  # type: ignore[arg-type]
