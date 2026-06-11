"""Tests for the site-generation read queries in db.py."""

from datetime import date, datetime, timezone

import pytest

from sg_law_cookies import db
from sg_law_cookies.models import Cookie, Source


@pytest.fixture()
def conn(tmp_path):
    conn = db.init_db(tmp_path / "test.db")
    yield conn
    conn.close()


def make_source(**overrides) -> Source:
    defaults = dict(
        source_url="https://www.judiciary.gov.sg/judgments/abc-v-def",
        zeeker_url="https://data.zeeker.sg/zeeker-judgements/judgments/1",
        title="ABC v DEF",
        raw_text="full text",
        date=date(2026, 6, 10),
        source_id="zeeker-judgements",
        item_type="judgment",
        license="Singapore Courts terms",
        token_count=1000,
    )
    defaults.update(overrides)
    return Source(**defaults)


def make_cookie(**overrides) -> Cookie:
    defaults = dict(
        headline="A headline",
        summary="A summary.",
        why_it_matters="It matters.",
        significance="medium",
        created_at=datetime(2026, 6, 11, 6, 2, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Cookie(**defaults)


def test_list_cookie_dates_distinct_desc(conn):
    days = [
        datetime(2026, 6, 8, 7, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 11, 6, 2, tzinfo=timezone.utc),
        datetime(2026, 6, 10, 5, 30, tzinfo=timezone.utc),
        datetime(2026, 6, 10, 9, 45, tzinfo=timezone.utc),  # same day twice
    ]
    for dt in days:
        db.save_cookie(conn, make_cookie(created_at=dt))

    assert db.list_cookie_dates(conn) == ["2026-06-11", "2026-06-10", "2026-06-08"]


def test_list_cookie_dates_empty(conn):
    assert db.list_cookie_dates(conn) == []


def test_cookies_for_date_filters_by_day_and_includes_duplicates(conn):
    on_day_1 = make_cookie(
        headline="first", created_at=datetime(2026, 6, 11, 5, 0, tzinfo=timezone.utc)
    )
    on_day_2 = make_cookie(
        headline="dup",
        is_duplicate=True,
        duplicate_of=on_day_1.id,
        created_at=datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc),
    )
    off_day = make_cookie(
        headline="other day",
        created_at=datetime(2026, 6, 10, 6, 0, tzinfo=timezone.utc),
    )
    for cookie in (on_day_2, off_day, on_day_1):
        db.save_cookie(conn, cookie)

    result = db.cookies_for_date(conn, date(2026, 6, 11))
    assert [c.id for c in result] == [on_day_1.id, on_day_2.id]  # oldest first
    assert result[1].is_duplicate is True
    assert isinstance(result[0], Cookie)

    assert db.cookies_for_date(conn, date(2026, 6, 9)) == []


def test_sources_for_cookies_maps_via_join(conn):
    src_a1 = make_source(
        source_url="https://www.judiciary.gov.sg/judgments/a1",
        ingested_at=datetime(2026, 6, 11, 1, 0, tzinfo=timezone.utc),
    )
    src_a2 = make_source(
        source_url="https://www.straitstimes.com/singapore/a2",
        source_id="sglawwatch",
        item_type="news",
        ingested_at=datetime(2026, 6, 11, 2, 0, tzinfo=timezone.utc),
    )
    src_b = make_source(source_url="https://www.mlaw.gov.sg/news/b")
    for src in (src_a1, src_a2, src_b):
        db.upsert_source(conn, src)

    cookie_a = make_cookie(source_ids=[src_a1.id, src_a2.id])
    cookie_b = make_cookie(source_ids=[src_b.id])
    cookie_none = make_cookie()  # no sources linked
    for cookie in (cookie_a, cookie_b, cookie_none):
        db.save_cookie(conn, cookie)

    result = db.sources_for_cookies(
        conn, [cookie_a.id, cookie_b.id, cookie_none.id]
    )
    assert set(result) == {cookie_a.id, cookie_b.id, cookie_none.id}
    # ordered by ingested_at: a1 before a2
    assert [s.id for s in result[cookie_a.id]] == [src_a1.id, src_a2.id]
    assert [s.id for s in result[cookie_b.id]] == [src_b.id]
    assert result[cookie_none.id] == []
    # the source_url field is the ORIGINAL url, not Zeeker
    for sources in result.values():
        for source in sources:
            assert "data.zeeker.sg" not in source.source_url
            assert isinstance(source, Source)


def test_sources_for_cookies_empty_input(conn):
    assert db.sources_for_cookies(conn, []) == {}


def test_latest_unresolved_terms_for_day(conn):
    day = date(2026, 6, 11)
    earlier = date(2026, 6, 9)
    db.record_unresolved_terms(conn, ["old-term"], seen=earlier)
    db.record_unresolved_terms(conn, ["whistleblowing", "deepfake evidence"], seen=day)
    db.record_unresolved_terms(conn, ["whistleblowing"], seen=day)  # count -> 2

    assert db.latest_unresolved_terms(conn, day) == [
        "whistleblowing",
        "deepfake evidence",
    ]
    assert db.latest_unresolved_terms(conn, date(2026, 6, 12)) == []
