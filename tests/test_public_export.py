"""Tests for the public export (data.zeeker.sg dataset) — sg_law_cookies.public_export."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sg_law_cookies import db
from sg_law_cookies.public_export import _sha12, export_public_db


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "cookies.db")
    # autocommit: the export runs on its own read-only connection,
    # so uncommitted fixture inserts would be invisible to it
    connection.isolation_level = None
    yield connection
    connection.close()


def make_source(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str = "Test Source",
    item_type: str = "news",
    doc_date: str = "2026-08-20",
) -> str:
    sid = _sha12(url)
    conn.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            sid,
            url,
            "https://data.zeeker.sg/sglawwatch/headlines/1",
            title,
            "raw text",
            doc_date,
            "sglawwatch",
            item_type,
            "CC-BY",
            100,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return sid


def add_live_cookie(
    conn: sqlite3.Connection,
    *,
    cookie_id: str,
    source_id: str,
    admitted: datetime,
    headline: str = "Test cookie",
    is_duplicate: int = 0,
    folio_areas: str = "[]",
) -> None:
    conn.execute(
        """
        INSERT INTO cookies (id, headline, summary, why_it_matters, significance,
                             folio_areas, folio_entities, folio_concepts, unresolved,
                             is_duplicate, duplicate_of, created_at, admitted_at)
        VALUES (?, ?, 'summary', 'why', 'high', ?, '[]', '[]', '[]', ?, NULL, ?, ?)
        """,
        (
            cookie_id,
            headline,
            folio_areas,
            is_duplicate,
            (admitted - timedelta(hours=1)).isoformat(),
            admitted.isoformat(),
        ),
    )
    conn.execute(
        "INSERT INTO cookie_sources (cookie_id, source_id) VALUES (?, ?)",
        (cookie_id, source_id),
    )


def make_judgment_meta(
    conn: sqlite3.Connection,
    source_id: str,
    citation: str = "[2026] SGHC 999",
    court: str = "High Court",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO judgment_meta (source_id, citation, court, judges, parties,
                                   issues, legislation, cases_cited, orders, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            citation,
            court,
            json.dumps(["Judge A"]),
            json.dumps(["Applicant", "Respondent"]),
            json.dumps(
                [
                    {
                        "question": "Q1?",
                        "holding": "H1",
                        "reasoning": "R1",
                        "folio_concepts": [],
                    },
                    {
                        "question": "Q2?",
                        "holding": "H2",
                        "reasoning": "R2",
                        "folio_concepts": [],
                    }
                ]
            ),
            json.dumps([]),
            json.dumps([]),
            "It is so ordered.",
            now,
        ),
    )


class TestCookieVisibility:
    def test_only_live_nonduplicated_admitted_cookies_exported(self, conn, tmp_path):
        s1 = make_source(conn, url="https://src/1")
        s2 = make_source(conn, url="https://src/2")
        s3 = make_source(conn, url="https://src/3")
        admitted = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)

        add_live_cookie(conn, cookie_id="c-live", source_id=s1, admitted=admitted)
        add_live_cookie(
            conn,
            cookie_id="c-dup",
            source_id=s2,
            admitted=admitted,
            is_duplicate=1,
        )
        # created but never admitted (still in review staging)
        conn.execute(
            """
            INSERT INTO cookies (id, headline, summary, why_it_matters, significance,
                                 folio_areas, folio_entities, folio_concepts, unresolved,
                                 is_duplicate, duplicate_of, created_at, admitted_at)
            VALUES ('c-unadmitted', 'h', 's', 'w', 'low', '[]', '[]', '[]', '[]', 0, NULL, ?, NULL)
            """,
            (admitted.isoformat(),),
        )

        out = tmp_path / "out.db"
        result = export_public_db(tmp_path / "cookies.db", out)

        exported = sqlite3.connect(out)
        try:
            ids = [r[0] for r in exported.execute("SELECT id FROM cookies")]
        finally:
            exported.close()
        assert ids == ["c-live"]
        assert result.cookies == 1

    def test_multi_source_first_link_wins(self, conn, tmp_path):
        s1 = make_source(conn, url="https://src/1")
        s2 = make_source(conn, url="https://src/2")
        admitted = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        add_live_cookie(conn, cookie_id="c1", source_id=s2, admitted=admitted)
        conn.execute(
            "INSERT INTO cookie_sources (cookie_id, source_id) VALUES (?, ?)",
            ("c1", s1),
        )

        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)
        exported = sqlite3.connect(out)
        try:
            (source_url,) = exported.execute(
                "SELECT source_url FROM cookies WHERE id='c1'"
            ).fetchone()
        finally:
            exported.close()
        assert source_url == "https://src/1"


class TestColumnsAndIds:
    def test_cookie_row_shape_and_ids_preserved(self, conn, tmp_path):
        s1 = make_source(conn, url="https://src/1")
        folio_areas = json.dumps(
            [
                {
                    "iri": "https://folio.example/R1",
                    "preferred_label": "Civil Procedure",
                    "branch": "areas_of_law",
                    "confidence": 0.9,
                }
            ]
        )
        admitted = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        add_live_cookie(
            conn,
            cookie_id="abc123def456",
            source_id=s1,
            headline="CA clarifies abuse of process",
            admitted=admitted,
            folio_areas=folio_areas,
        )

        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)
        exported = sqlite3.connect(out)
        try:
            row = exported.execute(
                "SELECT id, headline, significance, date, item_type, primary_area, source_url, source_title FROM cookies"
            ).fetchone()
        finally:
            exported.close()
        assert row[1] == "CA clarifies abuse of process"  # headline preserved
        assert row[4] == "news"  # item_type from source
        assert row[5] == "Civil Procedure"  # primary_area = first area label
        assert row[6] == "https://src/1"  # source_url
        # id is the v2 cookie id, not re-derived
        assert row[0] == "abc123def456"
        # date is the admitted day (site daily-page grouping)
        assert row[3] == "2026-08-25"

    def test_judgment_ids_are_url_hashes_with_v1_continuity(self, conn, tmp_path):
        s1 = make_source(conn, url="https://www.judiciary.gov.sg/judgment/1", item_type="judgment")
        make_judgment_meta(conn, s1)
        admitted = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        add_live_cookie(conn, cookie_id="jc1", source_id=s1, admitted=admitted)

        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)
        exported = sqlite3.connect(out)
        try:
            jid = exported.execute("SELECT id FROM judgments").fetchone()[0]
            ids = [r[0] for r in exported.execute("SELECT id FROM judgment_issues ORDER BY issue_index")]
        finally:
            exported.close()
        assert jid == _sha12("https://www.judiciary.gov.sg/judgment/1")
        assert ids == [_sha12(f"{jid}::0"), _sha12(f"{jid}::1")]


class TestUnresolvedTerms:
    def test_unresolved_terms_hashed_and_copied(self, conn, tmp_path):
        db.record_unresolved_terms(conn, ["dispute resolution", "civil procedure"])
        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)
        exported = sqlite3.connect(out)
        try:
            rows = dict(exported.execute("SELECT term, id FROM unresolved_terms"))
        finally:
            exported.close()
        assert rows["dispute resolution"] == _sha12("dispute resolution")
        assert rows["civil procedure"] == _sha12("civil procedure")


class TestMetadataAndFts:
    def test_zeeker_updates_counts_match_actual_rows(self, conn, tmp_path):
        s1 = make_source(conn, url="https://src/1", item_type="judgment")
        make_judgment_meta(conn, s1)
        admitted = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        add_live_cookie(conn, cookie_id="c1", source_id=s1, admitted=admitted)
        db.record_unresolved_terms(conn, ["term one"])

        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)
        exported = sqlite3.connect(out)
        try:
            counts = dict(
                exported.execute("SELECT resource_name, record_count FROM _zeeker_updates")
            )
            actual = {
                "cookies": exported.execute("SELECT COUNT(*) FROM cookies").fetchone()[0],
                "judgments": exported.execute("SELECT COUNT(*) FROM judgments").fetchone()[0],
                "judgment_issues": exported.execute("SELECT COUNT(*) FROM judgment_issues").fetchone()[0],
                "unresolved_terms": exported.execute("SELECT COUNT(*) FROM unresolved_terms").fetchone()[0],
            }
        finally:
            exported.close()
        assert counts == actual

    def test_fts_triggers_maintain_index(self, conn, tmp_path):
        s1 = make_source(conn, url="https://src/1")
        admitted = datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc)
        add_live_cookie(
            conn,
            cookie_id="c1",
            source_id=s1,
            admitted=admitted,
            headline="Arbitration costs award",
        )
        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)

        exported = sqlite3.connect(out)
        try:
            hits = exported.execute(
                "SELECT rowid FROM cookies_fts WHERE cookies_fts MATCH 'arbitration'"
            ).fetchall()
            # insert/delete through the triggers to prove the wiring works
            exported.execute(
                "INSERT INTO cookies VALUES ('x', 'x', 'x', 'x', 'low', NULL, NULL, NULL, '[]', '[]', '[]', '[]', 0, 'u', 't')"
            )
            exported.execute("DELETE FROM cookies WHERE id='x'")
        finally:
            exported.close()
        assert len(hits) == 1

    def test_internal_tables_absent_from_public_export(self, conn, tmp_path):
        db.record_unresolved_terms(conn, ["t"])
        s1 = make_source(conn, url="https://src/1")
        add_live_cookie(
            conn,
            cookie_id="c1",
            source_id=s1,
            admitted=datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc),
        )
        out = tmp_path / "out.db"
        export_public_db(tmp_path / "cookies.db", out)
        exported = sqlite3.connect(out)
        try:
            tables = {
                r[0]
                for r in exported.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
        finally:
            exported.close()
        for forbidden in ("pending_cookies", "rejected_cookies", "sources", "cookie_sources", "judgment_meta"):
            assert forbidden not in tables