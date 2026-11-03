"""Approval provenance: how each live cookie was admitted, and by whom.

Every path into the live `cookies` table must durably record its
approval_method; human approvals also record the reviewer (Discord user
ID, like rejected_by) and the time.  Existing rows read as 'unknown' —
never inferred or backfilled.  Re-ingestion/replays cannot clear or
change recorded provenance, and the fields never reach any public
surface (they stay out of the Cookie model entirely).
"""

import importlib.util
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from sg_law_cookies import db, folio
from sg_law_cookies.models import Cookie, Source
from sg_law_cookies.news_approval import apply_pending_news
from sg_law_cookies.pipeline import process_news
from test_news_approval import draft, plan
from test_pipeline import StubAnthropic, make_raw_item, mock_folio, topic_dict

_SCRIPT = (
    Path(__file__).resolve().parent.parent / "scripts" / "cookies-approval-server.py"
)
_spec = importlib.util.spec_from_file_location("cookies_approval_server", _SCRIPT)
assert _spec is not None and _spec.loader is not None
approval_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(approval_server)


@pytest.fixture
def conn(tmp_path):
    connection = db.init_db(tmp_path / "cookies.db")
    yield connection
    connection.close()


@pytest.fixture(autouse=True)
def clear_folio():
    folio.clear_cache()
    yield
    folio.clear_cache()


def provenance(conn, cookie_id):
    """(approval_method, approved_by, approved_at) of a live cookie."""
    row = conn.execute(
        "SELECT approval_method, approved_by, approved_at FROM cookies WHERE id = ?",
        (cookie_id,),
    ).fetchone()
    assert row is not None, f"cookie {cookie_id} is not live"
    return tuple(row)


# ── the four admission paths ─────────────────────────────────────────


def test_discord_promotion_records_reviewer_and_time(conn):
    cookie = draft(conn, kind="judgment")
    before = datetime.now(timezone.utc)
    promoted = db.promote_pending_cookie(
        conn, cookie.id, approval_method=db.APPROVAL_METHOD_DISCORD,
        approved_by="123456789012345678",
    )
    assert promoted is not None
    method, reviewer, approved_at = provenance(conn, cookie.id)
    assert method == "discord_approval"
    assert reviewer == "123456789012345678"
    assert datetime.fromisoformat(approved_at) >= before


def test_discord_promotion_without_identity_leaves_reviewer_null(conn):
    cookie = draft(conn)
    db.promote_pending_cookie(conn, cookie.id, approval_method=db.APPROVAL_METHOD_DISCORD)
    method, reviewer, approved_at = provenance(conn, cookie.id)
    assert method == "discord_approval"
    assert reviewer is None  # never invented
    assert approved_at is not None


def test_sweep_records_sweep_method_distinguishable_from_news(conn):
    old = draft(conn, kind="judgment", headline="Old judgment",
                created=datetime.now(timezone.utc) - timedelta(hours=80))
    young = draft(conn, kind="judgment", headline="Young judgment")
    assert db.auto_approve_pending(conn, max_age_hours=72) == [old.id]
    method, reviewer, approved_at = provenance(conn, old.id)
    assert method == "sweep_72h"
    assert reviewer is None
    assert approved_at is not None
    assert conn.execute(
        "SELECT 1 FROM cookies WHERE id = ?", (young.id,)
    ).fetchone() is None  # still pending, not admitted


@respx.mock
def test_news_auto_approval_records_method(conn):
    mock_folio()
    with httpx.Client() as folio_client:
        cookie, = process_news(make_raw_item(), conn, StubAnthropic([[topic_dict()]]),
                               folio_client, auto_approve_news=True)
    method, reviewer, approved_at = provenance(conn, cookie.id)
    assert method == "news_auto"
    assert reviewer is None
    assert approved_at is not None


def test_news_backlog_batch_records_method(conn, tmp_path):
    cookie = draft(conn)
    expected = plan(conn)
    backup = tmp_path / "before.db"
    result = apply_pending_news(
        tmp_path / "cookies.db", backup, expected
    )
    assert result["approved_ids"] == [cookie.id]
    method, reviewer, approved_at = provenance(conn, cookie.id)
    assert method == "news_backlog"
    assert reviewer is None  # the command receives no reviewer identity
    assert approved_at is not None


# ── approval server wiring (Discord user ID -> approved_by) ─────────


def test_approval_server_passes_discord_user_id(tmp_path, monkeypatch):
    path = tmp_path / "server.db"
    seed = db.init_db(path)
    cookie = draft(seed, kind="judgment")
    row = db.get_pending_cookie(seed, cookie.id)
    fingerprint = db.pending_fingerprint(row)
    db.set_pending_discord_msg_id(seed, cookie.id, "444")
    seed.close()

    monkeypatch.setattr(approval_server, "get_db_conn",
                        lambda: (db.init_db(path), db))
    monkeypatch.setattr(approval_server, "_get_discord_token", lambda: None)
    interaction = {
        "type": 3,
        "data": {"custom_id": f"approve:{cookie.id}:{fingerprint}"},
        "message": {"id": "444", "embeds": []},
        "channel_id": "999",
        "member": {"user": {"id": "123456789012345678", "global_name": "A"}},
    }
    body, status = approval_server.handle_button_click(
        interaction, "123456789012345678", "A"
    )
    assert status == 200 and b"Approved" in body

    check = db.init_db(path)
    try:
        method, reviewer, approved_at = provenance(check, cookie.id)
        assert method == "discord_approval"
        assert reviewer == "123456789012345678"
        assert approved_at is not None
    finally:
        check.close()


def test_approval_server_without_member_identity_records_null(tmp_path, monkeypatch):
    path = tmp_path / "server.db"
    seed = db.init_db(path)
    cookie = draft(seed, kind="judgment")
    fingerprint = db.pending_fingerprint(db.get_pending_cookie(seed, cookie.id))
    seed.close()

    monkeypatch.setattr(approval_server, "get_db_conn",
                        lambda: (db.init_db(path), db))
    monkeypatch.setattr(approval_server, "_get_discord_token", lambda: None)
    interaction = {
        "type": 3,
        "data": {"custom_id": f"approve:{cookie.id}:{fingerprint}"},
        "message": {"id": "444", "embeds": []},
        "channel_id": "999",
        "member": {"user": {}},
    }
    body, status = approval_server.handle_button_click(interaction, "unknown", "unknown")
    assert status == 200

    check = db.init_db(path)
    try:
        method, reviewer, _ = provenance(check, cookie.id)
        assert method == "discord_approval"
        assert reviewer is None  # no identity received, none stored
    finally:
        check.close()


# ── honesty of existing rows and undeclared writes ───────────────────


def _make_legacy_db(tmp_path):
    """A pre-provenance legacy database (the production shape)."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.executescript(db._SCHEMA)
    raw.execute("ALTER TABLE cookies ADD COLUMN admitted_at TEXT")
    raw.execute(
        "INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", "Headline", "Summary", "Why", "high", "[]", "[]", "[]", "[]",
         0, None, "2026-06-01T23:30:00-02:00", "2026-06-01T23:30:00-02:00"),
    )
    raw.commit()
    raw.close()
    return path


def test_existing_rows_read_unknown_after_migration(tmp_path):
    path = _make_legacy_db(tmp_path)
    conn = db.init_db(path, migrate=True)
    try:
        assert provenance(conn, "c1") == ("unknown", None, None)
        # new admissions on the migrated DB record normally
        cookie = draft(conn, kind="judgment")
        db.promote_pending_cookie(conn, cookie.id,
                                  approval_method=db.APPROVAL_METHOD_DISCORD,
                                  approved_by="u1")
        method, reviewer, approved_at = provenance(conn, cookie.id)
        assert (method, reviewer) == ("discord_approval", "u1")
        assert approved_at is not None
    finally:
        conn.close()


def test_undeclared_direct_insert_records_unknown_and_no_time(conn):
    source = Source(source_url="https://example.org/a", zeeker_url="https://z/1",
                    title="S", raw_text="text", date=date.today(), source_id="t",
                    item_type="news", license="test", token_count=1)
    db.upsert_source(conn, source)
    cookie = Cookie(source_ids=[source.id], headline="H", summary="S",
                    why_it_matters="W", significance="low")
    db.save_cookie(conn, cookie)
    assert provenance(conn, cookie.id) == ("unknown", None, None)


# ── nothing overwrites provenance ────────────────────────────────────


def test_replay_and_maintenance_do_not_change_provenance(conn):
    cookie = draft(conn, kind="judgment")
    db.promote_pending_cookie(conn, cookie.id,
                              approval_method=db.APPROVAL_METHOD_DISCORD,
                              approved_by="reviewer-1")
    original = provenance(conn, cookie.id)

    # maintenance re-save with the default (undeclared) method
    fresh = db.get_cookie(conn, cookie.id)
    fresh.summary = "Re-tagged by maintenance"
    db.save_cookie(conn, fresh)
    assert provenance(conn, cookie.id) == original

    # a re-save that tries to declare a different path cannot change it
    fresh = db.get_cookie(conn, cookie.id)
    db.save_cookie(conn, fresh, approval_method=db.APPROVAL_METHOD_SWEEP,
                   approved_by="someone-else")
    assert provenance(conn, cookie.id) == original

    # the area backfill path (update_cookie_areas -> save_cookie)
    db.update_cookie_areas(conn, cookie.id, [])
    assert provenance(conn, cookie.id) == original

    # a duplicate pending row promoted again is a no-op for provenance
    again = Cookie(id=cookie.id, source_ids=fresh.source_ids,
                   headline=fresh.headline, summary=fresh.summary,
                   why_it_matters=fresh.why_it_matters, significance=fresh.significance,
                   created_at=fresh.created_at)
    db.save_pending_cookie(conn, again, "judgment")
    db.promote_pending_cookie(conn, cookie.id,
                              approval_method=db.APPROVAL_METHOD_SWEEP)
    assert provenance(conn, cookie.id) == original


def test_replayed_ingestion_keeps_provenance(conn):
    """A source replay that reaches save_cookie for an existing live cookie
    (e.g. the news backlog duplicate-corroboration path) cannot rewrite it."""
    cookie = draft(conn, kind="judgment")
    db.promote_pending_cookie(conn, cookie.id,
                              approval_method=db.APPROVAL_METHOD_DISCORD,
                              approved_by="reviewer-1")
    original = provenance(conn, cookie.id)
    live = db.get_cookie(conn, cookie.id)
    live.summary = live.summary + " corroborated"
    db.save_cookie(conn, live, approval_method=db.APPROVAL_METHOD_NEWS_BACKLOG)
    assert provenance(conn, cookie.id) == original


# ── the fixed vocabulary is enforced ─────────────────────────────────


def test_promotion_requires_a_declared_method(conn):
    cookie = draft(conn)
    with pytest.raises(TypeError):
        db.promote_pending_cookie(conn, cookie.id)


def test_rejects_invented_method_values(conn):
    source = Source(source_url="https://example.org/b", zeeker_url="https://z/2",
                    title="S", raw_text="text", date=date.today(), source_id="t",
                    item_type="news", license="test", token_count=1)
    db.upsert_source(conn, source)
    cookie = Cookie(source_ids=[source.id], headline="H", summary="S",
                    why_it_matters="W", significance="low")
    with pytest.raises(ValueError):
        db.save_cookie(conn, cookie, approval_method="made_up")
    pending = draft(conn, headline="Another")
    with pytest.raises(ValueError):
        db.promote_pending_cookie(conn, pending.id, approval_method="made_up")


def test_database_check_rejects_invented_method(conn):
    source = Source(source_url="https://example.org/c", zeeker_url="https://z/3",
                    title="S", raw_text="text", date=date.today(), source_id="t",
                    item_type="news", license="test", token_count=1)
    db.upsert_source(conn, source)
    cookie = Cookie(source_ids=[source.id], headline="H", summary="S",
                    why_it_matters="W", significance="low")
    db.save_cookie(conn, cookie)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE cookies SET approval_method='nonsense' WHERE id=?",
                     (cookie.id,))
    conn.rollback()


# ── the fields stay out of the public model ──────────────────────────


def test_provenance_stays_out_of_the_cookie_model(conn):
    cookie = draft(conn, kind="judgment")
    db.promote_pending_cookie(conn, cookie.id,
                              approval_method=db.APPROVAL_METHOD_DISCORD,
                              approved_by="123456789012345678")
    loaded = db.get_cookie(conn, cookie.id)
    dumped = loaded.model_dump()
    assert dumped == db.get_cookie(conn, cookie.id).model_dump()
    leaked = [k for k in dumped
              if "approval" in k or "approved" in k]
    assert leaked == []
    assert "123456789012345678" not in loaded.model_dump_json()