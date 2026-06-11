"""Environment-driven settings for the pipeline and CLI."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sg_law_cookies.extraction import DEFAULT_MODEL
from sg_law_cookies.folio import CONFIDENCE_THRESHOLD
from sg_law_cookies.llm import DEFAULT_OLLAMA_HOST, DEFAULT_OLLAMA_MODEL

DEFAULT_DB_PATH = "./cookies.db"


@dataclass(frozen=True)
class Settings:
    db_path: Path
    anthropic_api_key: str | None
    model: str
    folio_confidence_threshold: float
    llm_backend: str  # "anthropic" | "ollama"
    ollama_host: str
    ollama_model: str


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    if env is None:
        env = os.environ
    return Settings(
        db_path=Path(env.get("COOKIES_DB_PATH", DEFAULT_DB_PATH)),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY") or None,
        model=env.get("COOKIES_MODEL", DEFAULT_MODEL),
        folio_confidence_threshold=float(
            env.get("FOLIO_CONFIDENCE_THRESHOLD", str(CONFIDENCE_THRESHOLD))
        ),
        llm_backend=env.get("COOKIES_LLM_BACKEND", "anthropic"),
        ollama_host=env.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST),
        ollama_model=env.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
    )
