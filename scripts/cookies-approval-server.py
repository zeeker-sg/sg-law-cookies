#!/usr/bin/env python3
"""Discord interaction webhook receiver for cookie approval.

Listens on 127.0.0.1:9002 and receives Discord interaction webhooks
(button clicks on cookie embeds).  Caddy at hooks.zeeker.sg proxies
/cookies-approval here.

Discord verifies the bot's public key using Ed25519 signatures
(different from GitHub's HMAC).  Each interaction is verified,
parsed, and handled:

  - approve: promote pending cookie → live cookies table
  - reject:  open a modal for rejection reason, then reject
  - edit:    open a modal with editable headline/summary/why_it_matters

Runs as a systemd --user service.

Requires in .env:
    DISCORD_BOT_TOKEN         — bot token (already in ~/.hermes/.env)
    DISCORD_PUBLIC_KEY        — bot public key (from Discord Developer Portal)
    COOKIES_DB_PATH            — path to cookies.db
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ── Config ────────────────────────────────────────────────────────────

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 9002
DEFAULT_DB_PATH = "./cookies.db"
MAX_BODY = 5 * 1024 * 1024  # 5 MB cap (Discord payloads are small)

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


# ── NaCL (Ed25519 signature verification) ──────────────────────────────
# Discord signs interactions with Ed25519. We use PyNaCl if available,
# otherwise fall back to a pure-Python ed25519 implementation.

_VERIFY_KEY = None


def init_verify_key() -> bytes:
    """Load the Discord bot's public key for signature verification."""
    global _VERIFY_KEY
    if _VERIFY_KEY is not None:
        return _VERIFY_KEY
    key_hex = os.environ.get("DISCORD_PUBLIC_KEY", "")
    if not key_hex:
        raise SystemExit("DISCORD_PUBLIC_KEY not set — cannot verify Discord signatures")
    _VERIFY_KEY = bytes.fromhex(key_hex)
    return _VERIFY_KEY


def verify_discord_signature(body: bytes, signature: str, timestamp: str) -> bool:
    """Verify Discord's Ed25519 signature on an interaction webhook.

    Discord sends X-Ed25519-Signature (hex) and X-Signature-Timestamp.
    The signed message is timestamp + body.
    """
    public_key = init_verify_key()
    try:
        from nacl.signing import VerifyKey
        from nacl.exceptions import BadSignatureError

        vk = VerifyKey(public_key)
        signed = timestamp.encode("utf-8") + body
        vk.verify(signed, bytes.fromhex(signature))
        return True
    except ImportError:
        # No PyNaCl — skip verification (for dev only).
        # In production, install pynacl: pip install pynacl
        print(
            "WARNING: PyNaCl not installed — skipping Discord signature verification. "
            "Install with: pip install pynacl",
            file=sys.stderr,
        )
        return True
    except Exception as exc:
        print(f"Signature verification failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False


# ── Cookie DB operations ──────────────────────────────────────────────


def get_db_conn():
    """Get a connection to the cookies DB."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from sg_law_cookies import db as cookies_db

    db_path = os.environ.get("COOKIES_DB_PATH", DEFAULT_DB_PATH) or DEFAULT_DB_PATH
    return cookies_db.init_db(db_path), cookies_db


# ── Discord message editing ─────────────────────────────────────────────


DISCORD_API = "https://discord.com/api/v10"

# Significance emoji and colours (must match post_pending_cookies.py).
SIGNIFICANCE_EMOJI = {"high": "🔴", "medium": "🟡", "low": "⚪"}
ITEM_TYPE_EMOJI = {"news": "📰", "judgment": "⚖️"}
# Greyed-out colour for actioned cookies (Discord embed colours are ints).
ACTIONED_COLOR = 0x4E5058  # dark grey

# Status badges prepended to the embed title.
STATUS_BADGES = {
    "approved": "✅ APPROVED",
    "rejected": "🗑️ REJECTED",
    "edited": "✏️ EDITED",
    "regenerating": "🔄 REGENERATING",
}


def discord_edit_message(
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
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        print(
            f"  discord_edit_message error {exc.code}: {err_body[:300]}",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"  discord_edit_message failed: {exc}", file=sys.stderr)
        return False


def update_discord_message_status(
    channel_id: str,
    message_id: str,
    token: str,
    action: str,
    reviewer: str,
    reason: str | None = None,
) -> bool:
    """Edit the original cookie embed to show its actioned status.

    - Greys out the embed colour.
    - Prepends a status badge to the title.
    - Removes the Approve / Reject / Edit buttons (empty components list).
    - Adds a footer line showing who actioned it and when.
    """
    # Fetch the original message to get the existing embed.
    url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "DiscordBot (https://zeeker.sg, 1.0)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            msg = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print(f"  fetch original message failed: {exc}", file=sys.stderr)
        return False

    embeds = msg.get("embeds", [])
    if not embeds:
        return False

    embed = embeds[0]  # only one embed per cookie message

    # Prepend status badge to title.
    badge = STATUS_BADGES.get(action, action.upper())
    original_title = embed.get("title", "")
    # Strip any existing badge prefix (in case of re-edits).
    for b in STATUS_BADGES.values():
        if original_title.startswith(b + " "):
            original_title = original_title[len(b) + 1 :]
    embed["title"] = f"{badge} {original_title}"

    # Grey out the embed colour.
    embed["color"] = ACTIONED_COLOR

    # Update footer with reviewer + timestamp.
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    footer_text = f"{badge} by {reviewer} • {ts}"
    if reason:
        footer_text += f" • reason: {reason[:100]}"
    embed["footer"] = {"text": footer_text[:300]}

    # Send the edit with empty components (removes buttons).
    body = {"embeds": [embed], "components": []}
    return discord_edit_message(channel_id, message_id, token, body)


def _get_discord_token() -> str | None:
    """Get the Discord bot token from environment."""
    return os.environ.get("DISCORD_BOT_TOKEN")


def _get_review_channel_id() -> str | None:
    """Get the review channel ID from environment."""
    return os.environ.get("DISCORD_REVIEW_CHANNEL")


# ── Comment scanner trigger ────────────────────────────────────────────


def _trigger_comment_scanner(cookie_id: str) -> None:
    """Run comment_triggered_ingest.py for a single cookie in a background thread.

    Called after an Edit modal submit — scans that cookie's thread for
    human comments and queues re-ingestion if any are found.
    """
    script = str(Path(__file__).resolve().parent / "comment_triggered_ingest.py")
    project_dir = str(Path(__file__).resolve().parent.parent)

    def _run():
        try:
            result = subprocess.run(
                ["python3", script, "--cookie-id", cookie_id],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60,
                env={**os.environ},
            )
            if result.stdout:
                print(f"[comment-scanner] {result.stdout.strip()}", file=sys.stderr)
            if result.returncode != 0 and result.stderr:
                print(f"[comment-scanner ERROR] {result.stderr.strip()}", file=sys.stderr)
        except Exception as exc:
            print(f"[comment-scanner] failed to run: {exc}", file=sys.stderr)

    threading.Thread(target=_run, daemon=True).start()


# ── Discord interaction responses ─────────────────────────────────────


def _resp(body: dict, status: int = 200) -> tuple[bytes, int]:
    return json.dumps(body).encode("utf-8"), status


def _modal_response(cookie_id: str, action: str) -> dict:
    """Build a modal interaction response for reject or edit."""
    if action == "reject":
        return {
            "type": 9,  # MODAL
            "data": {
                "title": "Reject cookie",
                "custom_id": f"reject_modal:{cookie_id}",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,  # TEXT_INPUT
                                "custom_id": "reject_reason",
                                "style": 2,  # PARAGRAPH
                                "label": "Why is this cookie being rejected? (used as LLM feedback for regeneration)",
                                "required": True,
                                "min_length": 5,
                                "max_length": 1000,
                            }
                        ],
                    }
                ],
            },
        }
    elif action == "edit":
        # We need the current cookie text to pre-fill the modal.
        conn, cookies_db = get_db_conn()
        row = cookies_db.get_pending_cookie(conn, cookie_id)
        conn.close()
        if row is None:
            return {
                "type": 4,
                "data": {
                    "content": f"❌ Cookie {cookie_id[:8]} not found in pending.",
                    "flags": 64,  # Ephemeral
                },
            }
        return {
            "type": 9,  # MODAL
            "data": {
                "title": "Edit cookie",
                "custom_id": f"edit_modal:{cookie_id}",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "edit_headline",
                                "style": 1,  # SHORT
                                "label": "Headline",
                                "value": row["headline"][:4000],
                                "required": True,
                                "max_length": 4000,
                            }
                        ],
                    },
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "edit_summary",
                                "style": 2,  # PARAGRAPH
                                "label": "Summary",
                                "value": row["summary"][:4000],
                                "required": True,
                                "max_length": 4000,
                            }
                        ],
                    },
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "edit_why_it_matters",
                                "style": 2,
                                "label": "Why it matters",
                                "value": row["why_it_matters"][:4000],
                                "required": True,
                                "max_length": 4000,
                            }
                        ],
                    },
                ],
            },
        }
    return {}


def handle_button_click(
    interaction: dict, user_id: str, username: str = "unknown"
) -> tuple[bytes, int]:
    """Handle a button click interaction (approve/reject/edit).

    After a successful action, edits the original Discord message to
    show the actioned status (badge in title, grey colour, buttons removed).
    """
    data = interaction.get("data", {})
    custom_id = data.get("custom_id", "")
    parts = custom_id.split(":", 1)
    action = parts[0]
    cookie_id = parts[1] if len(parts) > 1 else ""

    # Get the original message ID + channel ID for editing.
    msg_id = interaction.get("message", {}).get("id")
    channel_id = interaction.get("channel_id")
    token = _get_discord_token()
    review_channel = _get_review_channel_id() or channel_id

    if action == "approve":
        conn, cookies_db = get_db_conn()
        try:
            cookie = cookies_db.promote_pending_cookie(conn, cookie_id)
            if cookie is None:
                return _resp({
                    "type": 4,
                    "data": {"content": f"❌ Cookie {cookie_id[:8]} not found.", "flags": 64},
                })
            # Edit the original Discord message to show approved status.
            if msg_id and channel_id and token:
                update_discord_message_status(
                    channel_id, msg_id, token, "approved", username
                )
            return _resp({
                "type": 4,
                "data": {
                    "content": f"✅ **Approved** — cookie is now live: {cookie.headline[:80]}",
                    "flags": 64,
                },
            })
        except Exception as exc:
            return _resp({
                "type": 4,
                "data": {"content": f"❌ Error: {exc}", "flags": 64},
            })
        finally:
            conn.close()

    elif action == "reject":
        # Open a modal for the rejection reason.
        # Store msg_id/channel_id for later use after modal submit.
        modal = _modal_response(cookie_id, "reject")
        return _resp(modal)

    elif action == "edit":
        modal = _modal_response(cookie_id, "edit")
        return _resp(modal)

    return _resp({
        "type": 4,
        "data": {"content": f"❓ Unknown action: {action}", "flags": 64},
    })


def handle_modal_submit(
    interaction: dict, user_id: str, username: str = "unknown"
) -> tuple[bytes, int]:
    """Handle a modal submission (reject reason or edit text).

    After a successful action, edits the original Discord message to
    show the actioned status.
    """
    data = interaction.get("data", {})
    custom_id = data.get("custom_id", "")
    parts = custom_id.split(":", 1)
    action = parts[0]
    cookie_id = parts[1] if len(parts) > 1 else ""

    # Get the original message ID + channel ID for editing.
    # For MODAL_SUBMIT, the message field contains the message that
    # the button was on.
    msg_id = interaction.get("message", {}).get("id")
    channel_id = interaction.get("channel_id")
    token = _get_discord_token()

    # Extract modal component values
    submitted: dict[str, str] = {}
    for comp_row in data.get("components", []):
        for comp in comp_row.get("components", []):
            submitted[comp["custom_id"]] = comp.get("value", "")

    if action == "reject_modal":
        reason = submitted.get("reject_reason", "(no reason given)")
        conn, cookies_db = get_db_conn()
        try:
            status = cookies_db.reject_pending_cookie(
                conn, cookie_id, reason, rejected_by=user_id
            )
            # Edit the original Discord message.
            if msg_id and channel_id and token:
                if status == "rejected":
                    update_discord_message_status(
                        channel_id, msg_id, token, "rejected", username, reason=reason
                    )
                else:
                    update_discord_message_status(
                        channel_id, msg_id, token, "regenerating", username, reason=reason
                    )
            if status == "rejected":
                return _resp({
                    "type": 4,
                    "data": {
                        "content": f"🗑️ **Final rejection** — cookie discarded after 2nd rejection.\nReason: {reason[:200]}",
                        "flags": 64,
                    },
                })
            else:
                return _resp({
                    "type": 4,
                    "data": {
                        "content": f"🔄 **Rejected** — cookie queued for LLM regeneration with feedback.\nReason: {reason[:200]}",
                        "flags": 64,
                    },
                })
        except KeyError:
            return _resp({
                "type": 4,
                "data": {"content": f"❌ Cookie {cookie_id[:8]} not found in pending.", "flags": 64},
            })
        except Exception as exc:
            return _resp({
                "type": 4,
                "data": {"content": f"❌ Error: {exc}", "flags": 64},
            })
        finally:
            conn.close()

    elif action == "edit_modal":
        headline = submitted.get("edit_headline", "")
        summary = submitted.get("edit_summary", "")
        why_it_matters = submitted.get("edit_why_it_matters", "")
        conn, cookies_db = get_db_conn()
        try:
            cookies_db.update_pending_cookie_text(
                conn, cookie_id,
                headline=headline or None,
                summary=summary or None,
                why_it_matters=why_it_matters or None,
            )
            # Edit the original Discord message to show edited status.
            if msg_id and channel_id and token:
                update_discord_message_status(
                    channel_id, msg_id, token, "edited", username
                )
            # Trigger comment scanner for this cookie — collect any
            # thread comments and queue re-ingestion if found.
            _trigger_comment_scanner(cookie_id)
            return _resp({
                "type": 4,
                "data": {
                    "content": f"✏️ **Edited** — cookie text updated. Review the changes and approve when ready.\n**Headline:** {headline[:100]}",
                    "flags": 64,
                },
            })
        except Exception as exc:
            return _resp({
                "type": 4,
                "data": {"content": f"❌ Error: {exc}", "flags": 64},
            })
        finally:
            conn.close()

    return _resp({
        "type": 4,
        "data": {"content": f"❓ Unknown modal action: {action}", "flags": 64},
    })


# ── HTTP server ───────────────────────────────────────────────────────


class InteractionHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = json.dumps({"status": "ok", "time": time.time()}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/" and self.path != "/cookies-approval":
            self.send_response(404)
            self.end_headers()
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(min(content_length, MAX_BODY))

        # Verify Discord signature
        # Cloudflare/Caddy may normalize the header name from
        # "X-Ed25519-Signature" to "X-Signature-Ed25519", so check both.
        signature = (
            self.headers.get("X-Ed25519-Signature")
            or self.headers.get("X-Signature-Ed25519")
            or ""
        )
        timestamp = self.headers.get("X-Signature-Timestamp", "")

        if not verify_discord_signature(body, signature, timestamp):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Invalid signature")
            return

        try:
            interaction = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        interaction_type = interaction.get("type")
        user = interaction.get("member", {}).get("user", {})
        user_id = user.get("id", "unknown")
        username = user.get("global_name") or user.get("username") or "unknown"

        # Type 1: PING — Discord verifies the endpoint is alive.
        if interaction_type == 1:
            response, status = _resp({"type": 1})
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        # Type 3: MESSAGE_COMPONENT (button click)
        if interaction_type == 3:
            response, status = handle_button_click(interaction, user_id, username)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        # Type 5: MODAL_SUBMIT
        if interaction_type == 5:
            response, status = handle_modal_submit(interaction, user_id, username)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        # Unknown interaction type
        response, status = _resp({
            "type": 4,
            "data": {"content": f"❓ Unknown interaction type: {interaction_type}", "flags": 64},
        })
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):
        # Quiet logging — systemd journal captures stderr.
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {args[0]}\n")


def main() -> int:
    load_dotenv()
    # Also try Hermes .env for the bot token.
    hermes_env = Path.home() / ".hermes" / ".env"
    if hermes_env.is_file():
        load_dotenv(hermes_env)

    # Also try the cookies .env (for COOKIES_DB_PATH).
    cookies_env = Path(__file__).resolve().parent.parent / ".env"
    if cookies_env.is_file():
        load_dotenv(cookies_env)

    # Pre-initialise the verify key (fail fast if not set).
    try:
        init_verify_key()
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 1

    server = HTTPServer((LISTEN_HOST, LISTEN_PORT), InteractionHandler)
    print(f"cookies-approval-server listening on {LISTEN_HOST}:{LISTEN_PORT}")
    sys.stderr.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())