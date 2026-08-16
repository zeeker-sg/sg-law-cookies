#!/usr/bin/env python3
"""Re-run LLM extraction for rejected cookies with rejection feedback.

Reads pending_cookies where review_status='regenerating', re-runs the
LLM extraction on the original source text with the rejection reason
as additional context, and writes the regenerated cookie back to
pending (with review_status='pending' so it gets re-posted to Discord).

Runs as a Hermes cron job (every 30 minutes).

Requires in .env (same as the main pipeline):
    COOKIES_LLM_BACKEND=ollama
    OLLAMA_MODEL, OLLAMA_HOST, etc.
    COOKIES_DB_PATH
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def main() -> int:
    load_dotenv()
    cookies_env = Path(__file__).resolve().parent.parent / ".env"
    if cookies_env.is_file():
        load_dotenv(cookies_env)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

    from sg_law_cookies import db
    from sg_law_cookies.config import load_settings
    from sg_law_cookies.llm import OllamaBackend, AnthropicBackend
    from sg_law_cookies.models import Cookie, TopicExtraction
    from sg_law_cookies.folio import resolve_topic
    from sg_law_cookies.prompts import NEWS_SYSTEM_PROMPT
    from sg_law_cookies.extraction import NEWS_EXTRACTION_TOOL

    settings = load_settings()
    conn = db.init_db(settings.db_path)

    regenerating = db.list_regenerating(conn)
    if not regenerating:
        print("no cookies pending regeneration")
        return 0

    print(f"regenerating {len(regenerating)} rejected cookie(s)")

    # Build the LLM backend
    if settings.llm_backend == "ollama":
        backend = OllamaBackend(
            model=settings.ollama_model, host=settings.ollama_host
        )
    elif settings.llm_backend == "anthropic" and settings.anthropic_api_key:
        import anthropic
        backend = AnthropicBackend(
            anthropic.Anthropic(api_key=settings.anthropic_api_key),
            model=settings.model,
        )
    else:
        print("error: no LLM backend configured", file=sys.stderr)
        return 1

    import httpx

    regenerated = 0
    failed = 0
    for row in regenerating:
        cookie_id = row["id"]
        reason = row["reject_reason"] or "(no reason given)"
        source_ids = json.loads(row["source_ids"])
        item_type = row["item_type"]

        # Get the original source text
        if not source_ids:
            print(f"  skip {cookie_id[:8]}: no source_ids", file=sys.stderr)
            failed += 1
            continue

        source = db.get_source(conn, source_ids[0])
        if source is None:
            print(f"  skip {cookie_id[:8]}: source not found", file=sys.stderr)
            failed += 1
            continue

        # Build a regeneration prompt with the rejection feedback
        from sg_law_cookies.models import RawItem

        raw_item = RawItem(
            source_url=source.source_url,
            zeeker_url=source.zeeker_url,
            title=source.title,
            raw_text=source.raw_text,
            date=source.date,
            source_id=source.source_id,
            item_type=source.item_type,
            license=source.license,
            extras={},
        )

        # Augment the system prompt with the rejection reason
        regen_system = (
            f"{NEWS_SYSTEM_PROMPT}\n\n"
            f"IMPORTANT: Your previous extraction was REJECTED by a human reviewer.\n"
            f"Rejection reason: {reason}\n\n"
            f"Please produce a corrected extraction that addresses this issue. "
            f"Be more careful with factual accuracy and avoid the specific "
            f"problem identified in the rejection reason."
        )

        try:
            # Re-run extraction with the augmented prompt
            data = backend.structured(
                system=regen_system,
                user=(
                    f"Source URL: {raw_item.source_url}\n"
                    f"Title: {raw_item.title}\n"
                    f"Date: {raw_item.date.isoformat()}\n\n"
                    f"Article text:\n{raw_item.raw_text}"
                ),
                schema=NEWS_EXTRACTION_TOOL["input_schema"],
                tool_name=NEWS_EXTRACTION_TOOL["name"],
            )

            topics_data = data.get("topics") if isinstance(data, dict) else data
            if not isinstance(topics_data, list):
                raise ValueError(f"LLM returned no topics: {data!r}")

            topics = [TopicExtraction.model_validate(t) for t in topics_data]

            # FOLIO resolution
            with httpx.Client(timeout=30.0) as folio_client:
                for topic in topics:
                    resolve_topic(topic, folio_client)

            # Write regenerated cookies back to pending
            for topic in topics:
                new_cookie = Cookie(
                    source_ids=source_ids,
                    headline=topic.headline,
                    summary=topic.summary,
                    why_it_matters=topic.why_it_matters,
                    significance=topic.significance,
                    folio_areas=topic.folio_areas,
                    folio_entities=topic.folio_entities,
                    folio_concepts=topic.folio_concepts,
                    unresolved=topic.unresolved,
                )
                # Save with a new ID (the old one was discarded)
                db.save_pending_cookie(
                    conn, new_cookie,
                    item_type=item_type,
                    source_url=source.source_url,
                )
                regenerated += 1
                print(f"  regenerated: {new_cookie.headline[:60]}")

            # Delete the old regenerating row
            conn.execute(
                "DELETE FROM pending_cookies WHERE id = ?", (cookie_id,)
            )
            conn.commit()

        except Exception as exc:
            print(f"  FAILED {cookie_id[:8]}: {exc}", file=sys.stderr)
            failed += 1

    print(f"regenerated {regenerated}, failed {failed}")
    conn.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())