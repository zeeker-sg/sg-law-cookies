#!/usr/bin/env python3
"""Scan thread replies on cookie embeds and trigger re-ingestion.

For each cookie embed in #cookies-review that has a thread, checks for
human (non-bot) messages in the thread.  If found, marks the cookie as
'regenerating' with the comment text as feedback — exactly like the
reject flow, but triggered by a comment instead of the Reject button.

The existing cookies-regenerate-rejected cron job then picks it up on
the next run and re-runs LLM extraction with the comment as feedback.

State tracking: a 'comment_processed' flag on the pending cookie prevents
re-processing.  The cookie's review_status changes to 'regenerating',
which is the natural idempotency guard — once regenerating, the scanner
skips it (the regenerate cron handles the rest of the lifecycle).

Requires in .env:
    DISCORD_BOT_TOKEN      — bot token
    DISCORD_REVIEW_CHANNEL — channel ID for #cookies-review
    COOKIES_DB_PATH        — path to cookies.db
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"
DEFAULT_DB_PATH = "./cookies.db"
DEFAULT_FETCH_LIMIT = 100  # channel messages to scan per run

# Badges that indicate the cookie is fully actioned and should be skipped.
# "✏️ EDITED" is NOT included — an edited cookie is still pending and
# can be commented on to trigger re-ingestion.
ACTIONED_BADGES = ("✅ APPROVED", "🗑️ REJECTED", "🔄 REGENERATING")

# All badges (for stripping from titles when re-badging).
ALL_BADGES = ("✅ APPROVED", "🗑️ REJECTED", "✏️ EDITED", "🔄 REGENERATING")

# Cookie ID pattern from embed footers: "Cookie ID: <8-char-prefix>"
COOKIE_ID_RE = re.compile(r"Cookie ID:\s*([a-f0-9]+)", re.IGNORECASE)

ACTIONED_COLOR = 0x4E5058  # dark grey


# ── .env loading ──────────────────────────────────────────────────────


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
) -> dict | list:
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
        print(f"  Discord API error {exc.code}: {err_body[:300]}", file=sys.stderr)
        return {}
    except Exception as exc:
        print(f"  Discord request failed: {exc}", file=sys.stderr)
        return {}


def fetch_channel_messages(
    channel_id: str, token: str, limit: int = 100
) -> list[dict]:
    """Fetch recent messages from a channel."""
    result = discord_request(
        "GET", f"/channels/{channel_id}/messages?limit={limit}", token
    )
    return result if isinstance(result, list) else []


def fetch_thread_messages(thread_id: str, token: str, limit: int = 50) -> list[dict]:
    """Fetch messages from a thread."""
    result = discord_request(
        "GET", f"/channels/{thread_id}/messages?limit={limit}", token
    )
    return result if isinstance(result, list) else []


def get_message(channel_id: str, message_id: str, token: str) -> dict | None:
    """Fetch a single message."""
    result = discord_request(
        "GET", f"/channels/{channel_id}/messages/{message_id}", token
    )
    return result if isinstance(result, dict) else None


def edit_message_embed(
    channel_id: str, message_id: str, token: str, body: dict
) -> bool:
    """PATCH a Discord message.  Returns True on success."""
    url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="PATCH",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://zeeker.sg, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status in (200, 204)
    except Exception as exc:
        print(f"  edit_message_embed failed: {exc}", file=sys.stderr)
        return False


# ── Cookie embed detection ────────────────────────────────────────────


def extract_cookie_id_from_embed(msg: dict) -> str | None:
    """Extract the cookie ID prefix from a Discord message embed footer.

    The footer text is 'Cookie ID: <8-char-prefix>'.
    """
    embeds = msg.get("embeds", [])
    if not embeds:
        return None
    footer_text = embeds[0].get("footer", {}).get("text", "")
    m = COOKIE_ID_RE.search(footer_text)
    if m:
        return m.group(1)
    return None


def is_actioned_embed(msg: dict) -> bool:
    """Check if the embed was already actioned (has a status badge)."""
    embeds = msg.get("embeds", [])
    if not embeds:
        return False
    title = embeds[0].get("title", "")
    return any(badge in title for badge in ACTIONED_BADGES)


def is_bot_message(msg: dict) -> bool:
    """Check if a message was sent by a bot."""
    return msg.get("author", {}).get("bot", False)


# ── DB helpers ────────────────────────────────────────────────────────


def get_db_conn():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from sg_law_cookies import db as cookies_db

    db_path = os.environ.get("COOKIES_DB_PATH", DEFAULT_DB_PATH) or DEFAULT_DB_PATH
    return cookies_db.init_db(db_path), cookies_db


def find_pending_by_prefix(conn, id_prefix: str):
    """Find a pending cookie whose ID starts with the given prefix."""
    rows = conn.execute(
        "SELECT * FROM pending_cookies WHERE id LIKE ?",
        (id_prefix + "%",),
    ).fetchall()
    return rows[0] if rows else None


# ── Embed status update ──────────────────────────────────────────────


def mark_embed_regenerating(
    channel_id: str, message_id: str, token: str, reviewer: str, comment: str
) -> bool:
    """Edit the cookie embed to show '🔄 REGENERATING' status with comment."""
    msg = get_message(channel_id, message_id, token)
    if not msg or not msg.get("embeds"):
        return False

    embed = msg["embeds"][0]

    # Strip any existing badge from title.
    original_title = embed.get("title", "")
    for badge in ALL_BADGES:
        if original_title.startswith(badge + " "):
            original_title = original_title[len(badge) + 1:]
    embed["title"] = f"🔄 REGENERATING {original_title}"
    embed["color"] = ACTIONED_COLOR

    # Footer with reviewer + comment excerpt + timestamp.
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    comment_short = comment[:150]
    footer_text = f"🔄 Comment by {reviewer} • {ts} • {comment_short}"
    embed["footer"] = {"text": footer_text[:300]}

    body = {"embeds": [embed], "components": []}
    return edit_message_embed(channel_id, message_id, token, body)


# ── Main logic ────────────────────────────────────────────────────────


def process_cookie_thread(
    channel_id: str,
    token: str,
    conn,
    cookies_db,
    cookie_id: str,
) -> int:
    """Scan a single cookie's thread for comments and trigger re-ingestion.

    Looks up the cookie by ID prefix, finds its embed in the channel,
    reads thread messages from humans, and if found, marks the cookie
    'regenerating' with the comment text as feedback.

    Returns the number of cookies queued for regeneration (0 or 1).
    """
    # Look up the cookie in pending_cookies.
    row = find_pending_by_prefix(conn, cookie_id)
    if row is None:
        print(f"  cookie {cookie_id} not found in pending — already promoted or deleted")
        return 0

    # Skip if already regenerating or rejected.
    review_status = row["review_status"] if "review_status" in row.keys() else None
    if review_status == "regenerating":
        print(f"  cookie {cookie_id} already regenerating — skipping")
        return 0

    # Find the embed for this cookie in the channel.
    messages = fetch_channel_messages(channel_id, token, limit=DEFAULT_FETCH_LIMIT)
    if not messages:
        print("no messages fetched from channel")
        return 0

    target_msg = None
    for msg in messages:
        if not msg.get("embeds"):
            continue
        prefix = extract_cookie_id_from_embed(msg)
        if prefix and cookie_id.startswith(prefix):
            target_msg = msg
            break

    if target_msg is None:
        print(f"  cookie {cookie_id} embed not found in channel")
        return 0

    # Check if this embed has a thread.
    thread = target_msg.get("thread")
    if not thread or not thread.get("id"):
        print(f"  cookie {cookie_id} has no thread — no comments to collect")
        return 0

    thread_id = thread["id"]
    embed_msg_id = target_msg["id"]

    # Fetch thread messages.
    thread_msgs = fetch_thread_messages(thread_id, token, limit=50)
    if not thread_msgs:
        print(f"  cookie {cookie_id} thread has no messages")
        return 0

    # Collect human (non-bot) messages.
    human_comments: list[tuple[str, str]] = []  # (author_name, content)
    for tmsg in thread_msgs:
        if is_bot_message(tmsg):
            continue
        author_name = (
            tmsg.get("author", {}).get("global_name")
            or tmsg.get("author", {}).get("username")
            or "unknown"
        )
        content = tmsg.get("content", "").strip()
        if content:
            human_comments.append((author_name, content))

    if not human_comments:
        print(f"  cookie {cookie_id} thread has no human comments")
        return 0

    # Use the most recent comment (Discord returns newest-first).
    reviewer, comment_text = human_comments[0]
    full_feedback = comment_text
    if len(human_comments) > 1:
        all_comments = "; ".join(
            f"{a}: {c}" for a, c in reversed(human_comments)
        )
        full_feedback = all_comments

    print(
        f"  comment on cookie {cookie_id} by {reviewer}: "
        f"{comment_text[:100]}"
    )

    # Mark the cookie as regenerating with the comment as feedback.
    try:
        conn.execute(
            """
            UPDATE pending_cookies
            SET review_status = 'regenerating',
                reject_reason = ?,
                discord_msg_id = NULL
            WHERE id = ?
            """,
            (full_feedback, row["id"]),
        )
        conn.commit()
    except Exception as exc:
        print(f"  ERROR updating cookie {cookie_id}: {exc}", file=sys.stderr)
        return 0

    # Grey out the embed and show regenerating status.
    mark_embed_regenerating(
        channel_id, embed_msg_id, token, reviewer, comment_text
    )

    print(f"  ✅ queued cookie {cookie_id} for re-ingestion")
    return 1


def process_channel(
    channel_id: str,
    token: str,
    conn,
    cookies_db,
) -> int:
    """Scan cookie embeds for thread comments and trigger re-ingestion.

    For each cookie embed that has a thread, reads thread messages from
    humans.  If found, marks the cookie 'regenerating' with the comment
    as feedback and updates the embed.

    Returns the number of cookies queued for regeneration.
    """
    messages = fetch_channel_messages(channel_id, token, limit=DEFAULT_FETCH_LIMIT)
    if not messages:
        print("no messages fetched from channel")
        return 0

    print(f"scanning {len(messages)} messages in channel {channel_id}")

    queued = 0

    for msg in messages:
        # Only interested in messages with embeds (cookie cards).
        if not msg.get("embeds"):
            continue

        # Skip already-actioned embeds.
        if is_actioned_embed(msg):
            continue

        # Extract cookie ID from embed footer.
        cookie_prefix = extract_cookie_id_from_embed(msg)
        if not cookie_prefix:
            continue

        # Check if this embed has a thread.
        thread = msg.get("thread")
        if not thread or not thread.get("id"):
            continue

        thread_id = thread["id"]
        embed_msg_id = msg["id"]

        # Look up the cookie in pending_cookies.
        row = find_pending_by_prefix(conn, cookie_prefix)
        if row is None:
            continue  # already promoted or deleted

        # Skip if already regenerating or rejected.
        review_status = row["review_status"] if "review_status" in row.keys() else None
        if review_status == "regenerating":
            continue

        # Fetch thread messages.
        thread_msgs = fetch_thread_messages(thread_id, token, limit=50)
        if not thread_msgs:
            continue

        # Collect human (non-bot) messages.
        human_comments: list[tuple[str, str]] = []  # (author_name, content)
        for tmsg in thread_msgs:
            if is_bot_message(tmsg):
                continue
            author_name = (
                tmsg.get("author", {}).get("global_name")
                or tmsg.get("author", {}).get("username")
                or "unknown"
            )
            content = tmsg.get("content", "").strip()
            if content:
                human_comments.append((author_name, content))

        if not human_comments:
            continue

        # Use the most recent comment (Discord returns newest-first).
        reviewer, comment_text = human_comments[0]
        full_feedback = comment_text
        if len(human_comments) > 1:
            all_comments = "; ".join(
                f"{a}: {c}" for a, c in reversed(human_comments)
            )
            full_feedback = all_comments

        print(
            f"  comment on cookie {cookie_prefix} by {reviewer}: "
            f"{comment_text[:100]}"
        )

        # Mark the cookie as regenerating with the comment as feedback.
        # This mirrors the reject_pending_cookie first-rejection flow:
        #   review_status='regenerating', reject_reason=comment,
        #   discord_msg_id=NULL (so it gets re-posted after regeneration).
        # We don't increment reject_count — comment-triggered regeneration
        # is a fresh cycle, not a rejection.
        try:
            conn.execute(
                """
                UPDATE pending_cookies
                SET review_status = 'regenerating',
                    reject_reason = ?,
                    discord_msg_id = NULL
                WHERE id = ?
                """,
                (full_feedback, row["id"]),
            )
            conn.commit()
        except Exception as exc:
            print(f"  ERROR updating cookie {cookie_prefix}: {exc}", file=sys.stderr)
            continue

        # Grey out the embed and show regenerating status.
        mark_embed_regenerating(
            channel_id, embed_msg_id, token, reviewer, comment_text
        )

        queued += 1
        print(f"  ✅ queued cookie {cookie_prefix} for re-ingestion")

    return queued


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan cookie embed threads for comments")
    parser.add_argument(
        "--cookie-id",
        help="Only scan this specific cookie (by ID prefix). If omitted, scans all embeds.",
    )
    args = parser.parse_args()

    load_dotenv()
    hermes_env = Path.home() / ".hermes" / ".env"
    if hermes_env.is_file():
        load_dotenv(hermes_env)
    cookies_env = Path(__file__).resolve().parent.parent / ".env"
    if cookies_env.is_file():
        load_dotenv(cookies_env)

    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel_id = os.environ.get("DISCORD_REVIEW_CHANNEL")

    if not token:
        print("error: DISCORD_BOT_TOKEN not set", file=sys.stderr)
        return 1
    if not channel_id:
        print("error: DISCORD_REVIEW_CHANNEL not set", file=sys.stderr)
        return 1

    conn, cookies_db = get_db_conn()

    if args.cookie_id:
        print(f"==> scanning thread for cookie {args.cookie_id}")
        queued = process_cookie_thread(
            channel_id, token, conn, cookies_db, args.cookie_id
        )
    else:
        print("==> scanning for thread comments on cookie embeds")
        queued = process_channel(channel_id, token, conn, cookies_db)

    if queued:
        print(f"==> {queued} cookie(s) queued for comment-triggered re-ingestion")
        print(
            "    The cookies-regenerate-rejected cron job will re-run LLM "
            "extraction with the comments as feedback."
        )
    else:
        print("==> no new comments requiring re-ingestion")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())