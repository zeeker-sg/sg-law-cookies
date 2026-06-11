"""Atom 1.0 feed for the latest cookies (PRD §2.5 attribution, §6 channels).

`build_feed` produces a plain dict; `render_feed` renders it through
templates/feed.xml.j2. Entry links always point at the cookie's ORIGINAL
source URL — never a Zeeker URL — per PRD §2.5.
"""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader

from sg_law_cookies import db
from sg_law_cookies.models import Cookie

FEED_TITLE = "SG Law Cookies"
FEED_AUTHOR = "SG Law Cookies"
MAX_ENTRIES = 20
ATTRIBUTION = (
    "Data: data.zeeker.sg. Cookies are plain-language summaries "
    "of public legal documents and are not legal advice."
)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _tag_authority(base_url: str) -> str:
    host = urlsplit(base_url).netloc or base_url.strip().strip("/")
    return host or "sg-law-cookies"


def _entry_id(authority: str, cookie: Cookie) -> str:
    day = cookie.created_at.date().isoformat()
    return f"tag:{authority},{day}:cookie/{cookie.id}"


def _latest_cookies(conn: sqlite3.Connection, limit: int = MAX_ENTRIES) -> list[Cookie]:
    """Last `limit` non-duplicate cookies, newest first."""
    picked: list[Cookie] = []
    for day_iso in db.list_cookie_dates(conn):
        day_cookies = db.cookies_for_date(conn, date.fromisoformat(day_iso))
        day_cookies.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        for cookie in day_cookies:
            if cookie.is_duplicate:
                continue
            picked.append(cookie)
            if len(picked) >= limit:
                return picked
    return picked


def build_feed(conn: sqlite3.Connection, base_url: str) -> dict:
    """Build the Atom feed context: last 20 cookies, newest first."""
    base = base_url.rstrip("/")
    authority = _tag_authority(base_url)
    cookies = _latest_cookies(conn)
    sources = db.sources_for_cookies(conn, [c.id for c in cookies])

    entries: list[dict] = []
    for cookie in cookies:
        cookie_sources = sources.get(cookie.id, [])
        if cookie_sources:
            # ORIGINAL document URL of the first source — never Zeeker (PRD §2.5).
            link = cookie_sources[0].source_url
        else:
            link = f"{base}/d/{cookie.created_at.date().isoformat()}/"
        entries.append(
            {
                "title": cookie.headline,
                "link": link,
                "id": _entry_id(authority, cookie),
                "updated": _rfc3339(cookie.created_at),
                "summary": f"{cookie.summary} Why it matters: {cookie.why_it_matters}",
                "author": FEED_AUTHOR,
            }
        )

    updated = (
        entries[0]["updated"] if entries else _rfc3339(datetime.now(timezone.utc))
    )
    return {
        "title": FEED_TITLE,
        "id": f"{base}/feed.xml",
        "site_url": f"{base}/",
        "feed_url": f"{base}/feed.xml",
        "updated": updated,
        "author": FEED_AUTHOR,
        "rights": ATTRIBUTION,
        "entries": entries,
    }


def render_feed(feed: dict) -> str:
    """Render the feed dict to Atom 1.0 XML."""
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=True,
        keep_trailing_newline=True,
    )
    return env.get_template("feed.xml.j2").render(feed=feed)
