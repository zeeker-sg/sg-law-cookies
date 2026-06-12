"""Tests for judgment metadata storage (PRD section 4.2, pseudocode section 3)."""

import sqlite3
from datetime import date, datetime, timezone

import pytest

from sg_law_cookies import db
from sg_law_cookies.models import (
    CaseCitation,
    FolioRef,
    JudgmentIssue,
    JudgmentMeta,
    Source,
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
        title="Tan v Lim",
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


def make_meta(source_id: str, **overrides) -> JudgmentMeta:
    defaults = dict(
        source_id=source_id,
        citation="[2026] SGHC 34",
        court=FolioRef(
            iri="https://openlegalstandard.org/ontology/sg-high-court",
            preferred_label="High Court of Singapore",
            branch="forums_venues",
            confidence=0.95,
        ),
        judges=["Tan J"],
        parties=["Tan Ah Kow", "Lim Bee Leng"],
        issues=[
            JudgmentIssue(
                question="Whether the contract was validly formed",
                holding="Yes",
                reasoning="Offer and acceptance were established on the emails.",
                folio_concepts=[
                    FolioRef(
                        iri="https://openlegalstandard.org/ontology/offer-acceptance",
                        preferred_label="Offer and Acceptance",
                        branch="concepts",
                        confidence=0.9,
                    )
                ],
            ),
            JudgmentIssue(
                question="Whether the exclusion clause applies",
                holding="No",
                reasoning="The clause did not cover negligence on its plain wording.",
            ),
            JudgmentIssue(question="Costs"),
        ],
        legislation=[
            FolioRef(
                iri="https://openlegalstandard.org/ontology/ucta",
                preferred_label="Unfair Contract Terms Act 1977",
                branch="authorities",
                confidence=0.85,
            )
        ],
        cases_cited=[
            CaseCitation(citation="[2020] SGCA 12", treatment="followed"),
            CaseCitation(
                citation="[2019] SGHC 99",
                treatment="distinguished",
                internal_ref="some-source-id",
            ),
            CaseCitation(citation="[1893] 1 QB 256", treatment="referred"),
        ],
        orders="Judgment for the plaintiff with costs.",
    )
    defaults.update(overrides)
    return JudgmentMeta(**defaults)


def test_round_trip_full_meta(conn):
    source = make_source()
    db.upsert_source(conn, source)
    meta = make_meta(source.id)

    db.save_judgment_meta(conn, source.id, meta)
    loaded = db.get_judgment_meta(conn, source.id)

    assert loaded == meta
    assert len(loaded.issues) == 3
    assert loaded.issues[0].folio_concepts[0].preferred_label == "Offer and Acceptance"
    assert loaded.cases_cited[1].internal_ref == "some-source-id"


def test_round_trip_minimal_meta_null_court(conn):
    source = make_source()
    db.upsert_source(conn, source)
    meta = JudgmentMeta(source_id=source.id, citation="[2026] SGDC 5")

    db.save_judgment_meta(conn, source.id, meta)
    loaded = db.get_judgment_meta(conn, source.id)

    assert loaded == meta
    assert loaded.court is None
    assert loaded.issues == []
    assert loaded.orders == ""


def test_get_judgment_meta_missing(conn):
    assert db.get_judgment_meta(conn, "no-such-id") is None


def test_save_judgment_meta_upserts(conn):
    source = make_source()
    db.upsert_source(conn, source)
    db.save_judgment_meta(conn, source.id, make_meta(source.id))
    updated = make_meta(source.id, citation="[2026] SGCA 7", orders="Appeal allowed.")

    db.save_judgment_meta(conn, source.id, updated)
    loaded = db.get_judgment_meta(conn, source.id)

    assert loaded.citation == "[2026] SGCA 7"
    assert loaded.orders == "Appeal allowed."
    assert conn.execute("SELECT count(*) FROM judgment_meta").fetchone()[0] == 1


def test_find_source_by_citation_hit_case_and_whitespace_insensitive(conn):
    source = make_source()
    db.upsert_source(conn, source)
    db.save_judgment_meta(conn, source.id, make_meta(source.id))

    for query in ("[2026] SGHC 34", "[2026] sghc 34", "  [2026]   SGHC\t34 "):
        found = db.find_source_by_citation(conn, query)
        assert found is not None, query
        assert found.id == source.id
        assert found.title == "Tan v Lim"


def test_find_source_by_citation_miss(conn):
    source = make_source()
    db.upsert_source(conn, source)
    db.save_judgment_meta(conn, source.id, make_meta(source.id))

    assert db.find_source_by_citation(conn, "[2025] SGHC 34") is None
    assert db.find_source_by_citation(conn, "") is None


def test_list_judgment_citations(conn):
    s1 = make_source(source_url="https://www.judiciary.gov.sg/judgment/1")
    s2 = make_source(source_url="https://www.judiciary.gov.sg/judgment/2")
    db.upsert_source(conn, s1)
    db.upsert_source(conn, s2)
    db.save_judgment_meta(conn, s1.id, make_meta(s1.id, citation="[2026] SGHC 34"))
    db.save_judgment_meta(conn, s2.id, make_meta(s2.id, citation="  [2026]  SGCA 7 "))

    citations = db.list_judgment_citations(conn)

    assert citations == {
        "[2026] sghc 34": s1.id,
        "[2026] sgca 7": s2.id,
    }


def test_list_judgment_citations_empty(conn):
    assert db.list_judgment_citations(conn) == {}


def test_init_db_migrates_existing_db_without_judgment_meta(tmp_path):
    """init_db on a pre-Phase-3 production DB adds judgment_meta, keeps data."""
    path = tmp_path / "cookies.db"

    old = sqlite3.connect(str(path))
    old.execute(
        """
        CREATE TABLE sources (
            id TEXT PRIMARY KEY, source_url TEXT NOT NULL, zeeker_url TEXT NOT NULL,
            title TEXT NOT NULL, raw_text TEXT NOT NULL, date TEXT NOT NULL,
            source_id TEXT NOT NULL,
            item_type TEXT NOT NULL CHECK (item_type IN ('news', 'judgment')),
            license TEXT NOT NULL, token_count INTEGER NOT NULL,
            ingested_at TEXT NOT NULL
        )
        """
    )
    old.execute(
        "INSERT INTO sources VALUES ('s1', 'u', 'z', 't', 'r', '2026-06-10', "
        "'sglawwatch', 'news', 'MIT', 10, '2026-06-11T08:00:00+00:00')"
    )
    old.commit()
    old.close()

    conn = db.init_db(path)
    try:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "judgment_meta" in tables
        # existing data untouched
        assert db.get_source(conn, "s1").title == "t"
        # judgment functions work on the migrated DB
        source = make_source()
        db.upsert_source(conn, source)
        db.save_judgment_meta(conn, source.id, make_meta(source.id))
        assert db.find_source_by_citation(conn, "[2026] SGHC 34").id == source.id
        # idempotent: running init_db again is a no-op
        db.init_db(path).close()
    finally:
        conn.close()
