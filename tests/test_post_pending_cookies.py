"""Tests for Discord embed title truncation — scripts/post_pending_cookies.py.

Discord rejects embeds whose title exceeds 256 UTF-16 code units with
error 50035 (BASE_TYPE_MAX_LENGTH). This bit for real on 2026-09-22:
a judgment cookie with a 313-char headline never posted to review and
would have silently auto-approved after 72h.
"""

import importlib.util
import json
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "post_pending_cookies.py"
)
_spec = importlib.util.spec_from_file_location("post_pending_cookies", _SCRIPT)
assert _spec is not None and _spec.loader is not None
ppc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ppc)


# ── truncate_title ────────────────────────────────────────────────────


def test_short_title_unchanged():
    assert ppc.truncate_title("⚖️ Short headline") == "⚖️ Short headline"


def test_exactly_256_units_unchanged():
    title = "A" * 256
    assert ppc._utf16_len(title) == 256
    assert ppc.truncate_title(title) == title


def test_over_limit_truncated_to_256_units():
    title = "A" * 300
    out = ppc.truncate_title(title)
    assert out == "A" * 255 + "…"
    assert ppc._utf16_len(out) == 256
    assert out.startswith("A" * 255) and out.endswith("…")


def test_non_bmp_emoji_counts_double():
    """A 256-char title with one non-BMP emoji is 257 UTF-16 units → must truncate."""
    title = "A" * 255 + "📄"
    assert len(title) == 256
    assert ppc._utf16_len(title) == 257
    out = ppc.truncate_title(title)
    assert ppc._utf16_len(out) <= 256


def test_emoji_never_overshoots_the_cap():
    """Truncation boundary: the last kept char may be a 2-unit emoji, never exceed."""
    title = "A" * 253 + "📄" + "B" * 10
    out = ppc.truncate_title(title)
    assert ppc._utf16_len(out) <= 256
    assert out == "A" * 253 + "📄" + "…"


# ── the real stuck cookie (744e69e2, 313-char headline) ──────────────


STUCK_HEADLINE = (
    "High Court holds in [2026] SGHC 191 that registered employers have a "
    "positive legal duty under the Employment of Foreign Manpower (Work "
    "Passes) Regulations to provide adequate food to foreign domestic "
    "workers, and may be criminally liable for abetment by omission where "
    "they knowingly fail to discharge this duty."
)


def test_stuck_cookie_headline_fits():
    assert len(STUCK_HEADLINE) == 313
    title = ppc.truncate_title(f"⚖️ {STUCK_HEADLINE}")
    assert ppc._utf16_len(title) <= 256
    assert title.startswith("⚖️ High Court holds")
    assert title.endswith("…")


# ── build_embed integration ──────────────────────────────────────────


def make_row(headline: str) -> dict:
    return {
        "id": "744e69e2-a0b7-4ccb-9e32-1ab5f79b8dc7",
        "headline": headline,
        "summary": "A summary well under the field limit.",
        "why_it_matters": "Why it matters text.",
        "significance": "high",
        "item_type": "judgment",
        "source_url": "https://example.com/judgment",
        "unresolved": "",
        "folio_areas": json.dumps([{"preferred_label": "Employment Law"}]),
        "created_at": "2026-09-22T00:38:19.070939+00:00",
    }


def test_build_embed_truncates_long_title():
    embed = ppc.build_embed(make_row(STUCK_HEADLINE))
    assert ppc._utf16_len(embed["title"]) <= 256
    assert embed["title"].endswith("…")


def test_build_embed_keeps_short_title():
    embed = ppc.build_embed(make_row("Short headline"))
    assert embed["title"] == "⚖️ Short headline"