"""Tests for the Counter Map sky data layer (skydata.py)."""

import json
from datetime import date, datetime, timezone

import pytest

import hashlib

from sg_law_cookies import db
from sg_law_cookies.models import (
    Cookie,
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    Source,
)
from sg_law_cookies.sitegen import build_daily_context
from sg_law_cookies.skydata import build_sky_day, build_sky_index

DAY = date(2026, 6, 11)
DAY_NEXT = date(2026, 6, 12)
DAY_OLD = date(2026, 6, 9)  # within the trailing-7-day window of both

LONG_HEADLINE = (
    "CA resets the test for wrongful dismissal in a landmark ruling that "
    "reshapes employment litigation strategy across Singapore"
)  # > 90 chars


def _ref(label: str, branch: str = "areas") -> FolioRef:
    return FolioRef(
        iri=f"https://folio.example/{label}",
        preferred_label=label,
        branch=branch,
        confidence=0.9,
    )


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
        folio_concepts=[],
        source_ids=[source.id] if source else [],
        created_at=datetime(2026, 6, 11, 1, 0, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    cookie = Cookie(**defaults)
    db.save_cookie(conn, cookie)
    return cookie


@pytest.fixture()
def conn(tmp_path):
    conn = db.init_db(tmp_path / "sky.db")
    yield conn
    conn.close()


@pytest.fixture()
def populated(conn):
    """Three days spanning the tie window.

    2026-06-09: Criminal Law cookie carrying concept "sentencing".
    2026-06-11 (DAY): multi-area high judgment cookie (also "sentencing"),
        Contract medium, area-less low, Criminal medium, plus a duplicate.
    2026-06-12: two Employment cookies only (low created first, high second).
    """
    d_old = datetime(2026, 6, 9, 1, 0, 0, tzinfo=timezone.utc)
    d1 = datetime(2026, 6, 11, 1, 0, 0, tzinfo=timezone.utc)
    d2 = datetime(2026, 6, 12, 1, 0, 0, tzinfo=timezone.utc)

    # day -2: shares concept "sentencing" with c-high; primary area differs
    _cookie(
        conn,
        _source(id="src-old", source_url="https://example.com/old"),
        id="c-old",
        headline="Old criminal cookie",
        folio_areas=[_ref("Criminal Law")],
        folio_concepts=[_ref("sentencing", "concepts")],
        created_at=d_old,
    )

    # day D: high judgment cookie spanning two areas
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
        headline=LONG_HEADLINE,
        significance="high",
        folio_areas=[_ref("Employment Law"), _ref("Contract Law")],
        folio_concepts=[
            _ref("sentencing", "concepts"),  # resolved (has iri)
            # unresolved placeholder -> must be excluded from v2 concepts
            FolioRef(
                iri=None,
                preferred_label="quantum meruit",
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
    # judgment metadata for c-high's source: citation differs from the source
    # title so the meta-preference is observable; 9 issues to test the cap of
    # 8, with an empty holding inside the cap.
    db.save_judgment_meta(
        conn,
        "src-judg",
        JudgmentMeta(
            source_id="src-judg",
            citation="[2026] SGCA 99",
            issues=[
                JudgmentIssue(
                    question=f"Issue {n}?",
                    holding="" if n == 3 else f"Holding {n}.",
                    reasoning="Because reasons.",
                )
                for n in range(1, 10)
            ],
        ),
    )
    _cookie(
        conn,
        _source(id="src-contract", source_url="https://example.com/contract"),
        id="c-contract",
        headline="Contract news cookie",
        folio_areas=[_ref("Contract Law")],
        created_at=d1.replace(hour=2),
    )
    # no folio areas AND no sources -> area "General", kind "news"
    _cookie(
        conn,
        None,
        id="c-noarea",
        headline="Area-less low cookie",
        significance="low",
        folio_areas=[],
        created_at=d1.replace(hour=3),
    )
    _cookie(
        conn,
        _source(id="src-crim", source_url="https://example.com/crim"),
        id="c-crim",
        headline="Criminal news cookie",
        folio_areas=[_ref("Criminal Law")],
        created_at=d1.replace(hour=4),
    )
    # duplicate-flagged: must be excluded everywhere
    _cookie(
        conn,
        _source(id="src-dup", source_url="https://example.com/dup"),
        id="c-dup",
        headline="DUPLICATE-HEADLINE-SHOULD-NOT-APPEAR",
        significance="high",
        is_duplicate=True,
        duplicate_of="c-crim",
        created_at=d1.replace(hour=5),
    )

    # day +1: Employment only; low created BEFORE high to test sig ordering
    _cookie(
        conn,
        _source(id="src-next-low", source_url="https://example.com/next-low"),
        id="c-next-low",
        headline="Next-day low cookie",
        significance="low",
        created_at=d2,
    )
    _cookie(
        conn,
        _source(id="src-next-high", source_url="https://example.com/next-high"),
        id="c-next-high",
        headline="Next-day high cookie",
        significance="high",
        created_at=d2.replace(hour=2),
    )
    return conn


def test_area_aggregation_and_general_fallback(populated):
    sky = build_sky_day(populated, DAY)
    # all four areas count 1 -> sorted by label
    assert [a["label"] for a in sky["areas"]] == [
        "Contract Law",
        "Criminal Law",
        "Employment Law",
        "General",
    ]
    assert all(a["count"] == 1 for a in sky["areas"])
    # primary area is the FIRST folio_area: c-high lands in Employment Law
    employment = next(a for a in sky["areas"] if a["label"] == "Employment Law")
    assert employment["cookies"][0]["sig"] == "high"
    # area-less cookie falls back to General (v1 keys; v2 adds more)
    general = next(a for a in sky["areas"] if a["label"] == "General")
    [entry] = general["cookies"]
    assert entry["h"] == "Area-less low cookie"
    assert entry["sig"] == "low"
    assert entry["kind"] == "news"


def test_totals_and_high_counts(populated):
    sky = build_sky_day(populated, DAY)
    assert sky["date"] == "2026-06-11"
    assert sky["totals"] == {"total": 4, "news": 3, "judgments": 1, "high": 1}
    employment = next(a for a in sky["areas"] if a["label"] == "Employment Law")
    assert employment["high"] == 1
    assert all(
        a["high"] == 0 for a in sky["areas"] if a["label"] != "Employment Law"
    )


def test_kind_from_first_source_and_sourceless_fallback(populated):
    sky = build_sky_day(populated, DAY)
    by_area = {a["label"]: a for a in sky["areas"]}
    assert by_area["Employment Law"]["cookies"][0]["kind"] == "judgment"
    assert by_area["Contract Law"]["cookies"][0]["kind"] == "news"
    assert by_area["General"]["cookies"][0]["kind"] == "news"  # no sources


def test_headline_truncated_to_90_chars(populated):
    sky = build_sky_day(populated, DAY)
    employment = next(a for a in sky["areas"] if a["label"] == "Employment Law")
    h = employment["cookies"][0]["h"]
    assert len(h) == 90
    assert h == LONG_HEADLINE[:90]


def test_tie_from_multi_area_cookie(populated):
    sky = build_sky_day(populated, DAY)
    # c-high spans Employment + Contract -> +2; both areas present on DAY
    assert {"a": "Contract Law", "b": "Employment Law", "w": 2} in sky["ties"]


def test_tie_from_shared_concept_across_two_days(populated):
    sky = build_sky_day(populated, DAY)
    # "sentencing" appears on 06-09 (primary Criminal) and 06-11 (primary
    # Employment) within the trailing window -> +1; both present on DAY
    assert {"a": "Criminal Law", "b": "Employment Law", "w": 1} in sky["ties"]
    # deterministic ordering: -w then labels
    assert sky["ties"] == [
        {"a": "Contract Law", "b": "Employment Law", "w": 2},
        {"a": "Criminal Law", "b": "Employment Law", "w": 1},
    ]


def test_no_tie_when_one_area_absent_on_day(populated):
    # 2026-06-12 window still holds the Contract/Criminal pair weights, but
    # only Employment Law is present on the day itself -> no ties
    sky = build_sky_day(populated, DAY_NEXT)
    assert {a["label"] for a in sky["areas"]} == {"Employment Law"}
    assert sky["ties"] == []


def test_cookies_within_area_sorted_by_significance(populated):
    sky = build_sky_day(populated, DAY_NEXT)
    employment = next(a for a in sky["areas"] if a["label"] == "Employment Law")
    # high first despite being created after the low
    assert [c["sig"] for c in employment["cookies"]] == ["high", "low"]
    assert employment["high"] == 1


def test_duplicate_exclusion_matches_sitegen(populated):
    sky = build_sky_day(populated, DAY)
    assert "DUPLICATE-HEADLINE-SHOULD-NOT-APPEAR" not in json.dumps(sky)
    ctx = build_daily_context(populated, DAY)
    assert sky["totals"]["total"] == ctx["totals"]["total"]
    assert sky["totals"]["high"] == ctx["totals"]["high"]
    assert sky["totals"]["news"] == ctx["totals"]["news"]
    assert sky["totals"]["judgments"] == ctx["totals"]["judgments"]


def test_index_ascending_with_filtered_counts(populated):
    index = build_sky_index(populated)
    assert index == {
        "days": [
            {"date": "2026-06-09", "total": 1, "high": 0},
            {"date": "2026-06-11", "total": 4, "high": 1},  # duplicate excluded
            {"date": "2026-06-12", "total": 2, "high": 1},
        ]
    }


def test_empty_day_returns_empty_structure(conn):
    sky = build_sky_day(conn, DAY)
    assert sky == {
        "date": "2026-06-11",
        "totals": {"total": 0, "news": 0, "judgments": 0, "high": 0},
        "areas": [],
        "ties": [],
    }
    assert build_sky_index(conn) == {"days": []}


def test_output_is_byte_stable(populated):
    a = json.dumps(build_sky_day(populated, DAY), sort_keys=True)
    b = json.dumps(build_sky_day(populated, DAY), sort_keys=True)
    assert a == b


# ── v2 cookie payload ────────────────────────────────────────────────


def _entry(sky: dict, area: str, h_prefix: str) -> dict:
    area_d = next(a for a in sky["areas"] if a["label"] == area)
    return next(c for c in area_d["cookies"] if c["h"].startswith(h_prefix))


def test_v2_judgment_cookie_payload(populated):
    sky = build_sky_day(populated, DAY)
    entry = _entry(sky, "Employment Law", LONG_HEADLINE[:20])

    assert entry["id"] == hashlib.sha256(b"c-high").hexdigest()[:12]
    assert len(entry["id"]) == 12
    # h stays truncated; headline/summary/why are full
    assert entry["h"] == LONG_HEADLINE[:90]
    assert entry["headline"] == LONG_HEADLINE
    assert entry["summary"] == "A summary."
    assert entry["why"] == "It matters."
    assert entry["sig"] == "high"
    assert entry["kind"] == "judgment"
    # url is the ORIGINAL source url; src prefers the judgment_meta citation
    # over the "[2026] SGCA 17" parsed from the source title
    assert entry["url"] == "https://www.judiciary.gov.sg/judgments/abc-v-def"
    assert entry["src"] == "[2026] SGCA 99"


def test_v2_concepts_exclude_unresolved_keep_sg_local(populated):
    sky = build_sky_day(populated, DAY)
    entry = _entry(sky, "Employment Law", LONG_HEADLINE[:20])
    # resolved iri + sg_local kept, unresolved placeholder dropped, order kept
    assert entry["concepts"] == ["sentencing", "Ministry of Manpower"]
    # cookie with no concepts -> empty list, key still present
    assert _entry(sky, "Contract Law", "Contract news")["concepts"] == []


def test_v2_issues_capped_with_empty_holdings_omitted(populated):
    sky = build_sky_day(populated, DAY)
    entry = _entry(sky, "Employment Law", LONG_HEADLINE[:20])
    issues = entry["issues"]
    assert len(issues) == 8  # 9 in meta, capped
    assert issues[0] == {"q": "Issue 1?", "hold": "Holding 1."}
    assert issues[2] == {"q": "Issue 3?"}  # empty holding -> no "hold" key
    assert issues[-1] == {"q": "Issue 8?", "hold": "Holding 8."}
    # q + hold only: reasoning never leaks
    assert "Because reasons." not in json.dumps(issues)


def test_v2_news_cookie_has_no_issues_key(populated):
    sky = build_sky_day(populated, DAY)
    entry = _entry(sky, "Contract Law", "Contract news")
    assert "issues" not in entry
    assert entry["kind"] == "news"
    assert entry["url"] == "https://example.com/contract"
    assert entry["src"] == "SLW"  # sitegen's source_label for sglawwatch
    assert entry["id"] == hashlib.sha256(b"c-contract").hexdigest()[:12]


def test_v2_sourceless_cookie_falls_back_to_day_page(populated):
    sky = build_sky_day(populated, DAY)
    entry = _entry(sky, "General", "Area-less")
    assert entry["url"] == "/d/2026-06-11/"
    assert entry["src"] == "COOKIES"
    assert entry["concepts"] == []
    assert "issues" not in entry


def test_v2_v1_keys_unchanged(populated):
    sky = build_sky_day(populated, DAY)
    assert set(sky.keys()) == {"date", "totals", "areas", "ties"}
    for area in sky["areas"]:
        assert set(area.keys()) == {"label", "count", "high", "cookies"}
        for c in area["cookies"]:
            # v1 keys all present with v1 semantics
            assert len(c["h"]) <= 90
            assert c["sig"] in {"high", "medium", "low"}
            assert c["kind"] in {"news", "judgment"}
            # v2 keys present on every cookie
            assert {"id", "headline", "summary", "why", "url", "src", "concepts"} <= set(c)
