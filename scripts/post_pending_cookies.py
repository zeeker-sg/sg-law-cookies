#!/usr/bin/env python3
"""Post pending cookies to Discord for human-in-the-loop review.

Reads pending_cookies from the cookies SQLite DB where review_status='pending'
and discord_msg_id IS NULL (not yet posted), and posts each as a rich embed
with Approve / Reject / Edit buttons to a dedicated Discord channel.

Runs as a Hermes cron job (every 15-30 minutes).

Requires in .env:
    DISCORD_BOT_TOKEN    — bot token (already in ~/.hermes/.env)
    DISCORD_REVIEW_CHANNEL — channel ID for #cookies-review
    COOKIES_DB_PATH       — path to cookies.db (defaults to ./cookies.db)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────

DEFAULT_DB_PATH = "./cookies.db"
DISCORD_API = "https://discord.com/api/v10"

SIGNIFICANCE_EMOJI = {"high": "🔴", "medium": "🟡", "low": "⚪"}
SIGNIFICANCE_LABEL = {"high": "Act on", "medium": "Be aware of", "low": "Track"}
ITEM_TYPE_EMOJI = {"news": "📰", "judgment": "⚖️"}


def _env(key: str, default: str | None = None) -> str | None:
    val = os.environ.get(key, default)
    return val


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


# ── Discord API helpers ───────────────────────────────────────────────


def discord_request(
    method: str, path: str, token: str, body: dict | None = None
) -> dict:
    """Make a Discord API request and return the JSON response."""
    url = f"{DISCORD_API}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://zeeker.sg, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print(f"  Discord API error {exc.code}: {err_body[:200]}", file=sys.stderr)
        return {}
    except Exception as exc:
        print(f"  Discord request failed: {exc}", file=sys.stderr)
        return {}


def build_embed(row) -> dict:
    """Build a Discord embed for a pending cookie."""
    significance = row["significance"]
    item_type = row["item_type"]
    source_url = row["source_url"] or "(no source URL)"
    unresolved = json.loads(row["unresolved"]) if row["unresolved"] else []

    # Parse folio areas for display
    areas = []
    for ref in json.loads(row["folio_areas"]):
        areas.append(ref.get("preferred_label", "?"))
    areas_str = ", ".join(areas) if areas else "(none)"

    fields = [
        {"name": "Summary", "value": row["summary"], "inline": False},
        {"name": "Why it matters", "value": row["why_it_matters"], "inline": False},
        {"name": "Significance", "value": f"{SIGNIFICANCE_EMOJI.get(significance, '❓')} {SIGNIFICANCE_LABEL.get(significance, significance)}", "inline": True},
        {"name": "Areas", "value": areas_str, "inline": True},
        {"name": "Source", "value": source_url[:200], "inline": False},
    ]

    if unresolved:
        fields.append({
            "name": "⚠️ Unresolved FOLIO terms",
            "value": ", ".join(unresolved[:10]) + ("…" if len(unresolved) > 10 else ""),
            "inline": False,
        })

    color_map = {"high": 0xED4245, "medium": 0xFEE75C, "low": 0x57F287}
    emoji = ITEM_TYPE_EMOJI.get(item_type, "📄")
    title = f"{emoji} {row['headline']}"
    # Discord embed titles are capped at 256 chars (error 50035). Truncate
    # long headlines so the post still goes out instead of failing.
    if len(title) > 256:
        title = title[:255] + "…"
    embed = {
        "title": title,
        "fields": fields,
        "color": color_map.get(significance, 0x5865F2),
        "footer": {"text": f"Cookie ID: {row['id'][:8]}"},
        "timestamp": row["created_at"],
    }
    return embed


def build_components(cookie_id: str) -> list[dict]:
    """Build the Approve / Reject / Edit button row."""
    return [
        {
            "type": 1,  # ACTION_ROW
            "components": [
                {
                    "type": 2,  # BUTTON
                    "style": 3,  # SUCCESS (green)
                    "label": "✅ Approve",
                    "custom_id": f"approve:{cookie_id}",
                },
                {
                    "type": 2,
                    "style": 4,  # DANGER (red)
                    "label": "❌ Reject",
                    "custom_id": f"reject:{cookie_id}",
                },
                {
                    "type": 2,
                    "style": 1,  # PRIMARY (blue)
                    "label": "✏️ Edit",
                    "custom_id": f"edit:{cookie_id}",
                },
            ],
        }
    ]


# ── Main ───────────────────────────────────────────────────────────────


def main() -> int:
    load_dotenv()
    # Also try Hermes .env for the bot token.
    hermes_env = Path.home() / ".hermes" / ".env"
    if hermes_env.is_file():
        load_dotenv(hermes_env)

    token = _env("DISCORD_BOT_TOKEN")
    channel_id = _env("DISCORD_REVIEW_CHANNEL")
    db_path = _env("COOKIES_DB_PATH", DEFAULT_DB_PATH) or DEFAULT_DB_PATH

    if not token:
        print("error: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        return 1
    if not channel_id:
        print("error: DISCORD_REVIEW_CHANNEL not set", file=sys.stderr)
        return 1

    # Import the cookies DB module
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from sg_law_cookies import db as cookies_db

    conn = cookies_db.init_db(db_path)
    pending = cookies_db.list_pending_cookies(conn, status="pending", not_posted=True)

    if not pending:
        print(f"no new pending cookies to post (channel {channel_id})")
        return 0

    print(f"posting {len(pending)} pending cookie(s) to Discord channel {channel_id}")
    posted = 0
    for row in pending:
        embed = build_embed(row)
        components = build_components(row["id"])
        body = {
            "embeds": [embed],
            "components": components,
        }
        result = discord_request(
            "POST", f"/channels/{channel_id}/messages", token, body
        )
        msg_id = result.get("id")
        if msg_id:
            cookies_db.set_pending_discord_msg_id(conn, row["id"], msg_id)
            posted += 1
            print(f"  posted: {row['headline'][:60]} → msg {msg_id}")
            # Auto-create a thread on the cookie embed for comments.
            # Users can write feedback in the thread; the comment scanner
            # reads thread replies and queues the cookie for re-ingestion.
            thread_name = f"💬 Comments — {row['id'][:8]}"
            thread_body = {"name": thread_name, "auto_archive_duration": 10080}
            thread_result = discord_request(
                "POST",
                f"/channels/{channel_id}/messages/{msg_id}/threads",
                token,
                thread_body,
            )
            thread_id = thread_result.get("id")
            if thread_id:
                print(f"    thread created: {thread_name} → {thread_id}")
            else:
                print(f"    WARNING: could not create thread", file=sys.stderr)
        else:
            print(f"  FAILED to post: {row['headline'][:60]}", file=sys.stderr)

    print(f"posted {posted}/{len(pending)} cookies to Discord")
    conn.close()
    return 0 if posted == len(pending) else 1


if __name__ == "__main__":
    raise SystemExit(main())