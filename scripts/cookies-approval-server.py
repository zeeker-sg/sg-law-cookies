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
import sys
import time
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


def handle_button_click(interaction: dict, user_id: str) -> tuple[bytes, int]:
    """Handle a button click interaction (approve/reject/edit)."""
    data = interaction.get("data", {})
    custom_id = data.get("custom_id", "")
    parts = custom_id.split(":", 1)
    action = parts[0]
    cookie_id = parts[1] if len(parts) > 1 else ""

    if action == "approve":
        conn, cookies_db = get_db_conn()
        try:
            cookie = cookies_db.promote_pending_cookie(conn, cookie_id)
            if cookie is None:
                return _resp({
                    "type": 4,
                    "data": {"content": f"❌ Cookie {cookie_id[:8]} not found.", "flags": 64},
                })
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
        modal = _modal_response(cookie_id, "reject")
        return _resp(modal)

    elif action == "edit":
        modal = _modal_response(cookie_id, "edit")
        return _resp(modal)

    return _resp({
        "type": 4,
        "data": {"content": f"❓ Unknown action: {action}", "flags": 64},
    })


def handle_modal_submit(interaction: dict, user_id: str) -> tuple[bytes, int]:
    """Handle a modal submission (reject reason or edit text)."""
    data = interaction.get("data", {})
    custom_id = data.get("custom_id", "")
    parts = custom_id.split(":", 1)
    action = parts[0]
    cookie_id = parts[1] if len(parts) > 1 else ""

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
            response, status = handle_button_click(interaction, user_id)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
            return

        # Type 5: MODAL_SUBMIT
        if interaction_type == 5:
            response, status = handle_modal_submit(interaction, user_id)
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