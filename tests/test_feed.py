"""Tests for the Atom feed (feed.py + templates/feed.xml.j2)."""

import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest

from sg_law_cookies import db
from sg_law_cookies.feed import ATTRIBUTION, MAX_ENTRIES, build_feed, render_feed
from sg_law_cookies.models import Cookie, Source

ATOM_NS = "http://www.w3.org/2005/Atom"
A = f"{{{ATOM_NS}}}"
BASE_URL = "https://cookies.example.sg"


@pytest.fixture()
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def add_cookie(conn, *, headline, created_at, source_url, is_duplicate=False):
    source = Source(
        source_url=source_url,
        zeeker_url="https://data.zeeker.sg/sglawwatch/articles/1",
        title=headline,
        raw_text="text",
        date=created_at.date(),
        source_id="sglawwatch",
        item_type="news",
        license="terms",
        token_count=500,
    )
    db.upsert_source(conn, source)
    cookie = Cookie(
        source_ids=[source.id],
        headline=headline,
        summary="Summary & details.",
        why_it_matters="Because reasons.",
        significance="medium",
        is_duplicate=is_duplicate,
        created_at=created_at,
    )
    db.save_cookie(conn, cookie)
    return cookie


def test_feed_is_valid_atom_with_original_source_links(conn):
    base_dt = datetime(2026, 6, 11, 6, 0, tzinfo=timezone.utc)
    add_cookie(
        conn,
        headline="Oldest <cookie> & co",
        created_at=base_dt - timedelta(days=2),
        source_url="https://www.straitstimes.com/singapore/story-1",
    )
    add_cookie(
        conn,
        headline="Middle cookie",
        created_at=base_dt - timedelta(days=1),
        source_url="https://www.judiciary.gov.sg/judgments/2026-sghc-1",
    )
    newest = add_cookie(
        conn,
        headline="Newest cookie",
        created_at=base_dt,
        source_url="https://www.mlaw.gov.sg/news/press-releases/x",
    )

    feed = build_feed(conn, BASE_URL)
    xml = render_feed(feed)
    root = ET.fromstring(xml)  # parses => well-formed, incl. escaping of & and <

    assert root.tag == f"{A}feed"  # correct Atom 1.0 namespace
    assert root.find(f"{A}title").text == "SG Law Cookies"
    assert root.find(f"{A}author/{A}name").text == "SG Law Cookies"

    # attribution + disclaimer are carried in the feed itself
    rights = root.find(f"{A}rights").text
    assert "data.zeeker.sg" in rights
    assert "not legal advice" in rights
    assert rights == ATTRIBUTION

    entries = root.findall(f"{A}entry")
    assert len(entries) == 3

    # newest first
    titles = [e.find(f"{A}title").text for e in entries]
    assert titles == ["Newest cookie", "Middle cookie", "Oldest <cookie> & co"]

    expected_hosts = {
        "www.mlaw.gov.sg",
        "www.judiciary.gov.sg",
        "www.straitstimes.com",
    }
    seen_hosts = set()
    for entry in entries:
        href = entry.find(f"{A}link").attrib["href"]
        host = urlsplit(href).netloc
        assert "data.zeeker.sg" not in href  # links are ORIGINAL sources
        seen_hosts.add(host)
        assert entry.find(f"{A}id").text.startswith("tag:cookies.example.sg,")
        assert " Why it matters: Because reasons." in entry.find(f"{A}summary").text
        assert entry.find(f"{A}updated").text  # present, RFC3339 isoformat
    assert seen_hosts == expected_hosts

    # first entry's id is derived from the cookie id
    assert newest.id in entries[0].find(f"{A}id").text


def test_feed_caps_at_20_and_skips_duplicates(conn):
    base_dt = datetime(2026, 6, 11, 0, 0, tzinfo=timezone.utc)
    for i in range(25):
        add_cookie(
            conn,
            headline=f"Cookie {i}",
            created_at=base_dt + timedelta(hours=i),
            source_url=f"https://www.straitstimes.com/singapore/story-{i}",
        )
    add_cookie(
        conn,
        headline="Duplicate cookie",
        created_at=base_dt + timedelta(hours=30),
        source_url="https://www.straitstimes.com/singapore/dup",
        is_duplicate=True,
    )

    feed = build_feed(conn, BASE_URL + "/")  # trailing slash handled
    root = ET.fromstring(render_feed(feed))
    entries = root.findall(f"{A}entry")
    assert len(entries) == MAX_ENTRIES == 20

    titles = [e.find(f"{A}title").text for e in entries]
    assert "Duplicate cookie" not in titles
    # the 20 newest non-duplicates: Cookie 24 .. Cookie 5
    assert titles[0] == "Cookie 24"
    assert titles[-1] == "Cookie 5"


def test_feed_empty_db(conn):
    feed = build_feed(conn, BASE_URL)
    root = ET.fromstring(render_feed(feed))
    assert root.findall(f"{A}entry") == []
    assert root.find(f"{A}updated").text  # still has a valid updated stamp


def test_entry_without_sources_falls_back_to_day_page(conn):
    cookie = Cookie(
        headline="Sourceless",
        summary="S.",
        why_it_matters="W.",
        significance="low",
        created_at=datetime(2026, 6, 11, 6, 0, tzinfo=timezone.utc),
    )
    db.save_cookie(conn, cookie)
    feed = build_feed(conn, BASE_URL)
    assert feed["entries"][0]["link"] == f"{BASE_URL}/daily/2026-06-11/"
