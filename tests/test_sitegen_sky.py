"""Tests for sitegen's counter-map output: sky JSON, page, and doorways."""

import json
from datetime import date, datetime, timezone

import pytest

from sg_law_cookies import db
from sg_law_cookies.models import (
    Cookie,
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    Source,
)
from sg_law_cookies.sitegen import build_site

BASE_URL = "https://cookies.example.org"
CDN_MARKERS = ("jsdelivr", "unpkg", "cdnjs", "d3js.org")


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
    if source is not None:
        # Publication date = earliest source date; align it to the day the
        # cookie's created_at clearly intends so grouping files correctly.
        source = source.model_copy(update={"date": cookie.created_at.date()})
        db.upsert_source(conn, source)
    db.save_cookie(conn, cookie)
    return cookie


@pytest.fixture()
def conn(tmp_path):
    conn = db.init_db(tmp_path / "sky.db")
    yield conn
    conn.close()


@pytest.fixture()
def populated(conn):
    """Two days: 11 Jun (high judgment + medium + duplicate), 10 Jun (one medium)."""
    d1 = datetime(2026, 6, 11, 1, 0, 0, tzinfo=timezone.utc)
    d0 = datetime(2026, 6, 10, 1, 0, 0, tzinfo=timezone.utc)

    _cookie(
        conn,
        _source(
            id="src-judg",
            source_url="https://www.judiciary.gov.sg/judgments/abc-v-def",
            zeeker_url="https://data.zeeker.sg/zeeker-judgements/judgments/9",
            title="ABC v DEF [2026] SGCA 17",
            source_id="zeeker-judgements",
            item_type="judgment",
        ),
        id="c-high",
        headline="CA resets the test for wrongful dismissal",
        significance="high",
        folio_areas=[_ref("Employment Law"), _ref("Civil Procedure")],
        folio_concepts=[
            _ref("dismissal", "concepts"),  # resolved (has iri)
            # unresolved placeholder -> must NOT appear in emitted JSON
            FolioRef(
                iri=None,
                preferred_label="UNRESOLVED-CONCEPT-SHOULD-NOT-EMIT",
                branch="unresolved",
                confidence=0.0,
            ),
            # sg_local refs have no iri but count as resolved
            FolioRef(
                iri=None,
                preferred_label="Ministry of Manpower",
                branch="sg_local",
                confidence=1.0,
            ),
        ],
        created_at=d1,
    )
    db.save_judgment_meta(
        conn,
        "src-judg",
        JudgmentMeta(
            source_id="src-judg",
            citation="[2026] SGCA 17",
            issues=[
                JudgmentIssue(
                    question="Was the dismissal wrongful?",
                    holding="Yes; the test is recalibrated.",
                    reasoning="Because reasons.",
                )
            ],
        ),
    )
    _cookie(
        conn,
        _source(id="src-med", source_url="https://www.mom.gov.sg/newsroom/x",
                zeeker_url="https://data.zeeker.sg/sg-gov-newsrooms/mom_news/4",
                source_id="sg-gov-newsrooms"),
        id="c-med",
        headline="MOM updates work pass rules",
        significance="medium",
        created_at=d1.replace(hour=2),
    )
    _cookie(
        conn, _source(id="src-dup", source_url="https://example.com/dup-story"),
        id="c-dup",
        headline="DUPLICATE-HEADLINE-SHOULD-NOT-RENDER",
        is_duplicate=True,
        duplicate_of="c-med",
        created_at=d1.replace(hour=3),
    )
    _cookie(
        conn, _source(id="src-d0", source_url="https://example.com/old-story"),
        id="c-d0",
        headline="Older medium cookie",
        significance="medium",
        folio_areas=[_ref("Civil Procedure")],
        created_at=d0,
    )
    return conn


@pytest.fixture()
def built(populated, tmp_path):
    out = tmp_path / "dist"
    report = build_site(populated, out, BASE_URL)
    return out, report


def test_sky_index_lists_dates_ascending(built):
    out, _ = built
    index = json.loads((out / "data" / "sky" / "index.json").read_text())
    assert list(index.keys()) == ["days"]
    dates = [d["date"] for d in index["days"]]
    assert dates == ["2026-06-10", "2026-06-11"]  # ascending
    by_date = {d["date"]: d for d in index["days"]}
    assert by_date["2026-06-11"] == {"date": "2026-06-11", "total": 2, "high": 1}
    assert by_date["2026-06-10"] == {"date": "2026-06-10", "total": 1, "high": 0}


def test_per_day_json_matches_contract(built):
    out, _ = built
    day = json.loads((out / "data" / "sky" / "2026-06-11.json").read_text())
    assert set(day.keys()) == {"date", "totals", "areas", "ties"}
    assert day["date"] == "2026-06-11"
    assert day["totals"] == {"total": 2, "news": 1, "judgments": 1, "high": 1}

    assert day["areas"], "areas must not be empty"
    counts = [a["count"] for a in day["areas"]]
    assert counts == sorted(counts, reverse=True)  # desc by count
    V1_KEYS = {"h", "sig", "kind"}
    V2_KEYS = {"id", "headline", "summary", "why", "url", "src", "concepts"}
    for area in day["areas"]:
        assert set(area.keys()) == {"label", "count", "high", "cookies"}
        for c in area["cookies"]:
            assert V1_KEYS | V2_KEYS <= set(c.keys())
            assert len(c["h"]) <= 90
            assert c["sig"] in {"high", "medium", "low"}
            assert c["kind"] in {"news", "judgment"}
            assert len(c["id"]) == 12
            assert isinstance(c["url"], str) and c["url"]
            assert isinstance(c["concepts"], list)
            # issues key is judgment-only
            assert ("issues" in c) == (c["kind"] == "judgment")

    for tie in day["ties"]:
        assert set(tie.keys()) == {"a", "b", "w"}
        assert isinstance(tie["w"], int)

    # the duplicate-flagged cookie never reaches the sky data
    raw = (out / "data" / "sky" / "2026-06-11.json").read_text()
    assert "DUPLICATE-HEADLINE-SHOULD-NOT-RENDER" not in raw

    # every indexed date has its per-day file
    assert (out / "data" / "sky" / "2026-06-10.json").exists()


def test_per_day_json_v2_cookie_payloads(built):
    """sitegen-emitted JSON carries the v2 cookie fields end to end."""
    out, _ = built
    day = json.loads((out / "data" / "sky" / "2026-06-11.json").read_text())
    cookies = {c["headline"]: c for a in day["areas"] for c in a["cookies"]}

    judg = cookies["CA resets the test for wrongful dismissal"]
    assert judg["kind"] == "judgment"
    assert judg["url"] == "https://www.judiciary.gov.sg/judgments/abc-v-def"
    assert judg["src"] == "[2026] SGCA 17"  # judgment_meta citation
    assert judg["issues"] == [
        {"q": "Was the dismissal wrongful?", "hold": "Yes; the test is recalibrated."}
    ]
    # resolved iri + sg_local kept; unresolved placeholder dropped
    assert judg["concepts"] == ["dismissal", "Ministry of Manpower"]

    news = cookies["MOM updates work pass rules"]
    assert news["kind"] == "news"
    assert "issues" not in news
    assert news["url"] == "https://www.mom.gov.sg/newsroom/x"
    assert news["why"] == "It matters."
    assert news["summary"] == "A summary."

    # unresolved labels never reach the emitted file at all
    raw = (out / "data" / "sky" / "2026-06-11.json").read_text()
    assert "UNRESOLVED-CONCEPT-SHOULD-NOT-EMIT" not in raw


def test_counter_map_page_vendored_d3_no_cdn(built):
    out, _ = built
    page = out / "counter-map" / "index.html"
    assert page.exists()
    html = page.read_text()
    assert "/static/d3.v7.min.js" in html
    for marker in CDN_MARKERS:
        assert marker not in html, marker
    # page JS is external: the page references sky.js, which does the fetch
    assert '/static/sky.js' in html
    assert "kuih bangkit" in html
    # static assets the page needs were copied
    assert (out / "static" / "d3.v7.min.js").exists()
    assert (out / "static" / "sky.css").exists()
    assert (out / "static" / "sky.js").exists()
    assert (out / "static" / "site.css").exists()
    assert "/data/sky/index.json" in (out / "static" / "sky.js").read_text()


def test_nav_doorway_on_every_page(built):
    out, _ = built
    for rel in ("index.html", "counter-map/index.html", "daily/2026-06-11/index.html"):
        html = (out / rel).read_text()
        assert 'href="/counter-map/"' in html, rel
        assert "Counter Map" in html, rel


def test_daily_page_counter_link_and_pineapple_tart_link(built):
    out, _ = built
    html = (out / "index.html").read_text()
    assert "see how the day spread across the counter →" in html
    assert 'href="https://en.wikipedia.org/wiki/Pineapple_tart"' in html


def test_empty_db_still_builds_counter_map(conn, tmp_path):
    out = tmp_path / "dist-empty"
    build_site(conn, out, BASE_URL)
    assert (out / "counter-map" / "index.html").exists()
    index = json.loads((out / "data" / "sky" / "index.json").read_text())
    assert index == {"days": []}
