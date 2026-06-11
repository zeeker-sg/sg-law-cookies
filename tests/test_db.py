"""Tests for the SQLite storage layer."""

from datetime import date, datetime, timedelta, timezone

import pytest

from sg_law_cookies import db
from sg_law_cookies.models import (
    Cookie,
    DailyStats,
    FolioRef,
    Source,
    SourceRegistryEntry,
)


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "cookies.db")
    yield connection
    connection.close()


def make_source(**overrides) -> Source:
    defaults = dict(
        source_url="https://www.judiciary.gov.sg/judgment/1",
        zeeker_url="https://data.zeeker.sg/zeeker-judgements/judgments/1",
        title="Test v Test",
        raw_text="The court held...",
        date=date(2026, 6, 10),
        source_id="zeeker-judgements",
        item_type="judgment",
        license="CC-BY",
        token_count=1200,
        ingested_at=datetime(2026, 6, 11, 8, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Source(**defaults)


def make_cookie(**overrides) -> Cookie:
    defaults = dict(
        headline="CA clarifies abuse of process",
        summary="The Court of Appeal held that arbitral findings can ground abuse-of-process arguments.",
        why_it_matters="Changes how prior arbitral findings are deployed in litigation.",
        significance="high",
        folio_areas=[
            FolioRef(
                iri="https://openlegalstandard.org/ontology/civil-procedure",
                preferred_label="Civil Procedure",
                branch="areas_of_law",
                confidence=0.9,
            )
        ],
        folio_entities=[
            FolioRef(iri=None, preferred_label="PDPC", branch="unresolved", confidence=0.0)
        ],
        unresolved=["PDPC"],
        created_at=datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return Cookie(**defaults)


def test_init_db_creates_schema_and_wal(conn):
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "sources",
        "cookies",
        "cookie_sources",
        "daily_stats",
        "source_registry",
        "unresolved_terms",
    } <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "cookies.db"
    c1 = db.init_db(path)
    c1.close()
    c2 = db.init_db(path)
    c2.close()


def test_source_round_trip(conn):
    source = make_source()
    db.upsert_source(conn, source)
    found = db.find_source_by_url(conn, source.source_url)
    assert found == source

    # upsert updates in place
    db.upsert_source(conn, source.model_copy(update={"title": "Updated"}))
    found = db.find_source_by_url(conn, source.source_url)
    assert found.title == "Updated"
    assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_find_source_by_url_missing(conn):
    assert db.find_source_by_url(conn, "https://nowhere.example") is None


def test_cookie_round_trip_with_links(conn):
    s1 = make_source()
    s2 = make_source(
        id="src-2",
        source_url="https://www.straitstimes.com/article",
        source_id="sglawwatch",
        item_type="news",
    )
    db.upsert_source(conn, s1)
    db.upsert_source(conn, s2)

    cookie = make_cookie(source_ids=[s1.id])
    db.save_cookie(conn, cookie)

    loaded = db.get_cookie(conn, cookie.id)
    assert loaded == cookie
    assert loaded.folio_areas[0].iri == cookie.folio_areas[0].iri
    assert loaded.folio_entities[0].iri is None

    # add a corroborating source (many-to-many, PRD 2.1)
    db.link_cookie_source(conn, cookie.id, s2.id)
    loaded = db.get_cookie(conn, cookie.id)
    assert set(loaded.source_ids) == {s1.id, s2.id}

    # linking again is a no-op
    db.link_cookie_source(conn, cookie.id, s2.id)
    assert conn.execute("SELECT COUNT(*) FROM cookie_sources").fetchone()[0] == 2


def test_save_cookie_replaces_links(conn):
    s1 = make_source()
    db.upsert_source(conn, s1)
    cookie = make_cookie(source_ids=[s1.id])
    db.save_cookie(conn, cookie)
    db.save_cookie(conn, cookie)  # idempotent re-save
    assert conn.execute("SELECT COUNT(*) FROM cookie_sources").fetchone()[0] == 1


def test_find_recent_cookies(conn):
    today = date(2026, 6, 11)
    recent = make_cookie(
        created_at=datetime(2026, 6, 10, 9, 0, tzinfo=timezone.utc)
    )
    old = make_cookie(
        id="old-cookie",
        created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
    )
    db.save_cookie(conn, recent)
    db.save_cookie(conn, old)

    found = db.find_recent_cookies(conn, lookback_days=7, as_of=today)
    assert [c.id for c in found] == [recent.id]

    found = db.find_recent_cookies(conn, lookback_days=60, as_of=today)
    assert {c.id for c in found} == {recent.id, old.id}


def test_registry_and_watermarks(conn):
    entry = SourceRegistryEntry(
        zeeker_db="sglawwatch",
        table="headlines",
        pipeline="news",
        license="CC-BY",
    )
    db.upsert_registry_entry(conn, entry)

    assert db.get_watermark(conn, "sglawwatch", "headlines") is None
    db.set_watermark(conn, "sglawwatch", "headlines", "2026-06-11T00:00:00Z")
    assert db.get_watermark(conn, "sglawwatch", "headlines") == "2026-06-11T00:00:00Z"

    listed = db.list_registry(conn)
    assert len(listed) == 1
    assert listed[0].zeeker_db == "sglawwatch"
    assert listed[0].table == "headlines"
    assert listed[0].watermark == "2026-06-11T00:00:00Z"
    assert listed[0].active is True

    # upsert updates routing/licence without losing the PK
    db.upsert_registry_entry(
        conn, entry.model_copy(update={"license": "restricted", "active": False})
    )
    listed = db.list_registry(conn)
    assert len(listed) == 1
    assert listed[0].license == "restricted"
    assert db.list_registry(conn, active_only=True) == []


def test_get_watermark_unknown_table(conn):
    assert db.get_watermark(conn, "nope", "nope") is None


def test_set_watermark_unknown_table_raises(conn):
    with pytest.raises(KeyError):
        db.set_watermark(conn, "nope", "nope", "2026-01-01T00:00:00Z")


def test_record_unresolved_terms(conn):
    db.record_unresolved_terms(conn, ["PDPC", "CPF Board"], seen=date(2026, 6, 10))
    db.record_unresolved_terms(conn, ["PDPC"], seen=date(2026, 6, 11))

    terms = dict((t, (first, count)) for t, first, count in db.list_unresolved_terms(conn))
    assert terms["PDPC"] == (date(2026, 6, 10), 2)  # first_seen kept
    assert terms["CPF Board"] == (date(2026, 6, 10), 1)


def test_daily_stats_round_trip(conn):
    stats = DailyStats(
        date=date(2026, 6, 11),
        total_cookies=3,
        news_count=2,
        judgment_count=1,
        high_significance=["c1"],
        medium_significance=["c2"],
        areas_breakdown={"Employment Law": 2, "Civil Procedure": 1},
        courts_breakdown={"High Court": 1},
        busiest_area="Employment Law",
        unresolved_terms=["PDPC"],
    )
    db.save_daily_stats(conn, stats)
    assert db.get_daily_stats(conn, stats.date) == stats

    # upsert on same date
    db.save_daily_stats(conn, stats.model_copy(update={"total_cookies": 4}))
    assert db.get_daily_stats(conn, stats.date).total_cookies == 4
    assert conn.execute("SELECT COUNT(*) FROM daily_stats").fetchone()[0] == 1


def test_compute_daily_stats(conn):
    day = date(2026, 6, 11)
    news_src = make_source(
        id="src-news",
        source_url="https://news.example/1",
        source_id="sglawwatch",
        item_type="news",
    )
    judgment_src = make_source(id="src-judg")
    db.upsert_source(conn, news_src)
    db.upsert_source(conn, judgment_src)

    employment = FolioRef(
        iri="https://openlegalstandard.org/ontology/employment-law",
        preferred_label="Employment Law",
        branch="areas_of_law",
        confidence=1.0,
    )
    c1 = make_cookie(
        id="c1",
        source_ids=[news_src.id],
        significance="high",
        folio_areas=[employment],
        unresolved=["MOM circular"],
        created_at=datetime(2026, 6, 11, 9, 0, tzinfo=timezone.utc),
    )
    c2 = make_cookie(
        id="c2",
        source_ids=[news_src.id],
        significance="medium",
        folio_areas=[employment],
        unresolved=[],
        created_at=datetime(2026, 6, 11, 9, 5, tzinfo=timezone.utc),
    )
    c3 = make_cookie(
        id="c3",
        source_ids=[judgment_src.id],
        significance="low",
        created_at=datetime(2026, 6, 11, 9, 10, tzinfo=timezone.utc),
    )
    other_day = make_cookie(
        id="c-other",
        source_ids=[judgment_src.id],
        created_at=datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc),
    )
    for cookie in (c1, c2, c3, other_day):
        db.save_cookie(conn, cookie)

    stats = db.compute_daily_stats(conn, day)
    assert stats.date == day
    assert stats.total_cookies == 3
    assert stats.news_count == 2
    assert stats.judgment_count == 1
    assert stats.high_significance == ["c1"]
    assert stats.medium_significance == ["c2"]
    assert stats.areas_breakdown == {"Employment Law": 2, "Civil Procedure": 1}
    assert stats.busiest_area == "Employment Law"
    assert "MOM circular" in stats.unresolved_terms

    db.save_daily_stats(conn, stats)
    assert db.get_daily_stats(conn, day) == stats


def test_compute_daily_stats_empty_day(conn):
    stats = db.compute_daily_stats(conn, date(2026, 1, 1))
    assert stats.total_cookies == 0
    assert stats.busiest_area == ""
    assert stats.areas_breakdown == {}
