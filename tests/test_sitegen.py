"""Tests for the static site generator (sitegen.py + cookies build CLI)."""

import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

import pytest

from sg_law_cookies import db
from sg_law_cookies.models import Cookie, FolioRef, Source
from sg_law_cookies.sitegen import build_site, source_label

BASE_URL = "https://cookies.example.org"
ATOM_NS = "{http://www.w3.org/2005/Atom}"


def _ref(label: str, branch: str = "areas") -> FolioRef:
    return FolioRef(iri=f"https://folio.example/{label}", preferred_label=label,
                    branch=branch, confidence=0.9)


def _source(**overrides) -> Source:
    defaults = dict(
        source_url="https://www.straitstimes.com/sg/some-article",
        zeeker_url="https://data.zeeker.sg/sglawwatch/headlines/1",
        title="Some article",
        raw_text="text",
        date=date(2026, 6, 10),
        source_id="sglawwatch",
        item_type="news",
        license="all rights reserved",
        token_count=100,
    )
    defaults.update(overrides)
    return Source(**defaults)


def _cookie(conn, source: Source | None, **overrides) -> Cookie:
    if source is not None:
        db.upsert_source(conn, source)
    defaults = dict(
        headline="A headline",
        summary="A summary.",
        why_it_matters="It matters.",
        significance="medium",
        folio_areas=[_ref("Employment Law")],
        folio_concepts=[_ref("dismissal", "concepts")],
        source_ids=[source.id] if source else [],
        created_at=datetime(2026, 6, 11, 22, 2, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    cookie = Cookie(**defaults)
    db.save_cookie(conn, cookie)
    return cookie


@pytest.fixture()
def conn(tmp_path):
    conn = db.init_db(tmp_path / "site.db")
    yield conn
    conn.close()


@pytest.fixture()
def populated(conn):
    """Two days of cookies: 11 Jun (high+medium+low+dup+area-less), 10 Jun (mediums only)."""
    d1 = datetime(2026, 6, 11, 1, 0, 0, tzinfo=timezone.utc)
    d0 = datetime(2026, 6, 10, 1, 0, 0, tzinfo=timezone.utc)

    judgment_src = _source(
        id="src-judg",
        source_url="https://www.judiciary.gov.sg/judgments/abc-v-def",
        zeeker_url="https://data.zeeker.sg/zeeker-judgements/judgments/9",
        title="ABC v DEF [2026] SGCA 17",
        source_id="zeeker-judgements",
        item_type="judgment",
    )
    _cookie(
        conn, judgment_src,
        id="c-high",
        headline="CA resets the test for wrongful dismissal",
        summary="The Court of Appeal restated the test.",
        why_it_matters="Changes pleading strategy.",
        significance="high",
        created_at=d1,
    )
    _cookie(
        conn, _source(id="src-med1", source_url="https://www.mom.gov.sg/newsroom/x",
                      zeeker_url="https://data.zeeker.sg/sg-gov-newsrooms/mom_news/4",
                      source_id="sg-gov-newsrooms"),
        id="c-med1",
        headline="MOM updates work pass rules",
        significance="medium",
        created_at=d1.replace(hour=2),
    )
    # medium with NO folio areas -> menu group "General"
    _cookie(
        conn, _source(id="src-med2", source_url="https://www.pdpc.gov.sg/decisions/y",
                      zeeker_url="https://data.zeeker.sg/pdpc/enforcement_decisions/2",
                      source_id="pdpc"),
        id="c-med2",
        headline="PDPC fines a retailer",
        significance="medium",
        folio_areas=[],
        created_at=d1.replace(hour=3),
    )
    _cookie(
        conn, _source(id="src-low", source_url="https://example.com/low-story"),
        id="c-low",
        headline="Routine procedural update",
        significance="low",
        folio_areas=[_ref("Civil Procedure")],
        created_at=d1.replace(hour=4),
    )
    # duplicate-flagged cookie must not appear anywhere
    _cookie(
        conn, _source(id="src-dup", source_url="https://example.com/dup-story"),
        id="c-dup",
        headline="DUPLICATE-HEADLINE-SHOULD-NOT-RENDER",
        is_duplicate=True,
        duplicate_of="c-med1",
        created_at=d1.replace(hour=5),
    )
    db.record_unresolved_terms(conn, ["whistleblowing"], seen=date(2026, 6, 11))

    # day 0: mediums only, no highs -> no specials board
    _cookie(
        conn, _source(id="src-d0", source_url="https://example.com/old-story"),
        id="c-d0",
        headline="Older medium cookie",
        significance="medium",
        created_at=d0,
    )
    return conn


@pytest.fixture()
def built(populated, tmp_path):
    out = tmp_path / "dist"
    report = build_site(populated, out, BASE_URL)
    return out, report


def test_build_writes_expected_files(built):
    out, report = built
    for rel in (
        "index.html",
        "d/2026-06-11/index.html",
        "d/2026-06-10/index.html",
        "d/index.html",
        "about/index.html",
        "feed.xml",
        "static/site.css",
    ):
        assert (out / rel).exists(), rel
    assert report.dates == ["2026-06-11", "2026-06-10"]
    # 2 daily pages + homepage + archive + about + feed + counter map
    assert report.pages == 7


def test_index_attribution_disclaimer_and_source_links(built):
    out, _ = built
    html = (out / "index.html").read_text()
    assert "data.zeeker.sg" in html
    assert "not legal advice" in html
    # original source links present
    assert "https://www.judiciary.gov.sg/judgments/abc-v-def" in html
    assert "https://www.mom.gov.sg/newsroom/x" in html
    # duplicate-flagged cookie never renders
    assert "DUPLICATE-HEADLINE-SHOULD-NOT-RENDER" not in html


def test_no_read_source_anchor_points_at_zeeker(built):
    out, _ = built
    for page in out.rglob("*.html"):
        html = page.read_text()
        hrefs = re.findall(r'href="([^"]+)"', html)
        for href in hrefs:
            if "data.zeeker.sg" in href:
                # the only zeeker link allowed is the bare attribution link
                assert href.rstrip("/") == "https://data.zeeker.sg", (page, href)


def test_daily_page_content(built):
    out, _ = built
    html = (out / "d" / "2026-06-11" / "index.html").read_text()
    assert "CA resets the test for wrongful dismissal" in html  # specials
    assert "Today&#39;s Specials" in html or "Today's Specials" in html
    assert "EMPLOYMENT LAW" in html                              # ticker/busiest
    assert "WHISTLEBLOWING" in html                              # new ingredient
    assert "MOM" in html                                         # newsroom acronym
    assert "[2026] SGCA 17" in html                              # judgment label
    assert "General" in html                                     # area-less medium group
    assert "civil procedure (1)" in html                         # rack breakdown
    assert 'href="/d/2026-06-10/"' in html                       # prev nav


def test_no_high_sig_day_has_no_specials_board(built):
    out, _ = built
    html = (out / "d" / "2026-06-10" / "index.html").read_text()
    assert "Specials" not in html
    assert 'class="board' not in html


def test_feed_parses_as_atom_with_original_links(built):
    out, _ = built
    tree = ET.parse(out / "feed.xml")
    root = tree.getroot()
    assert root.tag == f"{ATOM_NS}feed"
    entries = root.findall(f"{ATOM_NS}entry")
    assert len(entries) == 5  # duplicates excluded
    for entry in entries:
        link = entry.find(f"{ATOM_NS}link").get("href")
        assert "data.zeeker.sg" not in link


def test_empty_db_builds_warming_up_homepage(conn, tmp_path):
    out = tmp_path / "dist-empty"
    report = build_site(conn, out, BASE_URL)
    html = (out / "index.html").read_text()
    assert "warming up" in html
    assert "data.zeeker.sg" in html
    assert "not legal advice" in html
    assert (out / "feed.xml").exists()
    assert (out / "d" / "index.html").exists()
    assert report.warnings


def test_archive_lists_days(built):
    out, _ = built
    html = (out / "d" / "index.html").read_text()
    assert 'href="/d/2026-06-11/"' in html
    assert 'href="/d/2026-06-10/"' in html
    assert "Thursday, 11 June 2026" in html


def test_source_label_mapping():
    assert source_label(_source()) == "SLW"
    assert source_label(_source(source_id="pdpc")) == "PDPC"
    assert (
        source_label(
            _source(
                source_id="sg-gov-newsrooms",
                zeeker_url="https://data.zeeker.sg/sg-gov-newsrooms/mlaw_news/3",
            )
        )
        == "MLAW"
    )
    assert (
        source_label(
            _source(source_id="zeeker-judgements", title="ABC v DEF [2026] SGHC 102")
        )
        == "[2026] SGHC 102"
    )
    assert (
        source_label(_source(source_id="zeeker-judgements", title="ABC v DEF"))
        == "JUDICIARY"
    )


def test_cli_build_prints_page_count(populated, tmp_path, monkeypatch, capsys):
    from sg_law_cookies import cli

    db_path = tmp_path / "cli.db"
    # point the CLI at the populated DB by copying via backup
    dest = db.init_db(db_path)
    populated.backup(dest)
    dest.close()
    monkeypatch.setenv("COOKIES_DB_PATH", str(db_path))
    out = tmp_path / "cli-dist"
    rc = cli.main(["build", "--out", str(out), "--base-url", BASE_URL])
    assert rc == 0
    captured = capsys.readouterr()
    assert "built 7 pages" in captured.out
    assert (out / "index.html").exists()
