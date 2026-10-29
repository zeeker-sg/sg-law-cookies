"""News admission tests: no real providers, publishing, or production databases."""
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from pydantic import ValidationError

from sg_law_cookies import db, folio
from sg_law_cookies.cli import main
from sg_law_cookies.config import load_settings
from sg_law_cookies.models import Cookie, Source
from sg_law_cookies.news_approval import (
    NewsApprovalError, apply_pending_news, open_news_db, plan_pending_news,
)
from sg_law_cookies.pipeline import PipelineError, process_news
from test_pipeline import StubAnthropic, make_raw_item, mock_folio, topic_dict


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "cookies.db"
    conn = db.init_db(path)
    yield path, conn
    conn.close()


@pytest.fixture(autouse=True)
def clear_folio():
    folio.clear_cache()
    yield
    folio.clear_cache()


def draft(conn, *, headline="A legal development", kind="news", status="pending",
          rejected=False, created=None, source_kind=None):
    source = Source(source_url="https://example.org/" + str(conn.total_changes),
                    zeeker_url="https://data.example/news/1", title="Source",
                    raw_text="Source article.", date=date.today(),
                    source_id="test", item_type=source_kind or kind,
                    license="test", token_count=3)
    db.upsert_source(conn, source)
    cookie = Cookie(source_ids=[source.id], headline=headline, summary="A summary.",
                    why_it_matters="It matters.", significance="medium",
                    unresolved=["Unresolved term remains a warning"],
                    created_at=created or datetime.now(timezone.utc))
    db.save_pending_cookie(conn, cookie, kind, source.source_url)
    conn.execute("UPDATE pending_cookies SET review_status=?,reject_count=?,"
                 "reject_reason=? WHERE id=?",
                 (status, int(rejected), "Reviewer feedback" if rejected else None, cookie.id))
    conn.commit()
    return cookie


def plan(conn):
    return plan_pending_news(conn).report()


def test_setting_is_default_off_and_opt_in():
    assert load_settings({}).auto_approve_news is False
    assert load_settings({"COOKIES_AUTO_APPROVE_NEWS": "true"}).auto_approve_news is True


@respx.mock
def test_future_news_admitted_with_unresolved_warnings_and_source_links(database):
    _, conn = database
    mock_folio()
    stub = StubAnthropic([[topic_dict()]])
    with httpx.Client() as folio:
        cookie, = process_news(make_raw_item(), conn, stub, folio, auto_approve_news=True)
    stored = db.get_cookie(conn, cookie.id)
    assert stored and stored.admitted_at and stored.source_ids == cookie.source_ids
    assert stored.unresolved and db.get_pending_cookie(conn, cookie.id) is None


@respx.mock
def test_disabled_setting_keeps_news_pending(database):
    _, conn = database
    mock_folio()
    with httpx.Client() as folio:
        cookie, = process_news(make_raw_item(), conn, StubAnthropic([[topic_dict()]]), folio)
    assert db.get_pending_cookie(conn, cookie.id) and db.get_cookie(conn, cookie.id) is None


@respx.mock
def test_raw_judgment_cannot_take_news_admission_path(database):
    _, conn = database
    mock_folio()
    with httpx.Client() as folio:
        cookie, = process_news(make_raw_item(item_type="judgment"), conn,
                               StubAnthropic([[topic_dict()]]), folio, auto_approve_news=True)
    assert db.get_pending_cookie(conn, cookie.id) and db.get_cookie(conn, cookie.id) is None


@respx.mock
def test_future_duplicate_preserves_flag_and_corroborating_sources(database):
    _, conn = database
    mock_folio()
    stub = StubAnthropic([[topic_dict("Same story")], [topic_dict("Same story")]])
    with httpx.Client() as folio:
        first, = process_news(make_raw_item("https://example.org/a"), conn, stub, folio,
                              auto_approve_news=True)
        second, = process_news(make_raw_item("https://example.org/b"), conn, stub, folio,
                               auto_approve_news=True)
    stored = db.get_cookie(conn, second.id)
    assert stored.is_duplicate and stored.duplicate_of == first.id
    assert len(db.get_cookie(conn, first.id).source_ids) == 2
    assert conn.execute("SELECT count(*) FROM cookies WHERE is_duplicate=0").fetchone()[0] == 1


@respx.mock
def test_future_retry_reuses_cookie_and_does_not_reapprove_existing_pending(database):
    _, conn = database
    mock_folio()
    stub = StubAnthropic([[topic_dict()]])
    raw = make_raw_item()
    with httpx.Client() as folio:
        first, = process_news(raw, conn, stub, folio)
        second, = process_news(raw, conn, stub, folio, auto_approve_news=True)
    assert first.id == second.id and len(stub.calls) == 1
    assert db.get_pending_cookie(conn, first.id) and db.get_cookie(conn, first.id) is None


@respx.mock
def test_admitted_retry_is_idempotent(database):
    _, conn = database
    mock_folio()
    stub = StubAnthropic([[topic_dict()]])
    with httpx.Client() as folio:
        first = process_news(make_raw_item(), conn, stub, folio, auto_approve_news=True)
        second = process_news(make_raw_item(), conn, stub, folio, auto_approve_news=True)
    assert len(stub.calls) == 1 and [c.id for c in first] == [c.id for c in second]


@pytest.mark.parametrize("bad", [{"significance": "invalid"}, {"summary": "   "}])
@respx.mock
def test_validation_failures_do_not_store_source_or_approve(database, bad):
    _, conn = database
    mock_folio()
    with httpx.Client() as folio, pytest.raises((PipelineError, ValidationError)):
        process_news(make_raw_item(), conn, StubAnthropic([[topic_dict(**bad)]]),
                     folio, auto_approve_news=True)
    assert conn.execute("SELECT count(*) FROM cookies").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 0


def test_backlog_only_valid_never_rejected_pending_news(database, tmp_path):
    path, conn = database
    news = draft(conn)
    judgment = draft(conn, kind="judgment", headline="Judgment")
    excluded = [draft(conn, status="rejected", headline="Rejected"),
                draft(conn, status="regenerating", headline="Regenerating"),
                draft(conn, rejected=True, headline="Edited after rejection"),
                draft(conn, source_kind="judgment", headline="Mislabeled"),
                draft(conn, headline=" ")]
    before = plan(conn)
    assert before["eligible_ids"] == [news.id] and len(before["skipped"]) == len(excluded)
    backup = tmp_path / "before.db"
    result = apply_pending_news(path, backup, before)
    assert result["approved_ids"] == [news.id] and backup.stat().st_mode & 0o777 == 0o600
    assert db.get_cookie(conn, news.id) and db.get_pending_cookie(conn, judgment.id)
    assert all(db.get_pending_cookie(conn, c.id) for c in excluded)
    saved = open_news_db(backup)
    assert db.get_pending_cookie(saved, news.id) and db.get_cookie(saved, news.id) is None
    saved.close()
    again = apply_pending_news(path, backup, before)
    assert again["approved_ids"] == [] and again["already_applied_ids"] == [news.id]


def test_backlog_duplicate_flags_and_source_links_are_preserved(database, tmp_path):
    path, conn = database
    first = draft(conn)
    second = draft(conn)
    before = plan(conn)
    assert before["duplicates"] == {second.id: first.id}
    apply_pending_news(path, tmp_path / "before.db", before)
    assert db.get_cookie(conn, second.id).duplicate_of == first.id
    assert db.get_cookie(conn, second.id).is_duplicate
    assert len(db.get_cookie(conn, first.id).source_ids) == 2


def test_duplicate_of_pending_judgment_stays_in_review(database):
    _, conn = database
    draft(conn, kind="judgment", created=datetime.now(timezone.utc)-timedelta(minutes=1))
    news = draft(conn)
    report = plan(conn)
    assert report["eligible_count"] == 0
    assert report["skipped"] == [{"id": news.id, "reason": "duplicate_of_pending_review"}]


def test_existing_live_duplicate_is_not_published_twice(database, tmp_path):
    path, conn = database
    original = draft(conn)
    db.promote_pending_cookie(conn, original.id)
    duplicate = draft(conn)
    apply_pending_news(path, tmp_path / "before.db", plan(conn))
    assert db.get_cookie(conn, duplicate.id).is_duplicate
    assert len(db.get_cookie(conn, original.id).source_ids) == 2
    assert conn.execute("SELECT count(*) FROM cookies WHERE is_duplicate=0").fetchone()[0] == 1


def test_failed_batch_rolls_back_every_promotion(database, tmp_path, monkeypatch):
    path, conn = database
    first = draft(conn)
    second = draft(conn, headline="Another development")
    before = plan(conn)
    actual = db.save_cookie
    count = 0
    def fail_second(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("simulated write failure")
        return actual(*args, **kwargs)
    monkeypatch.setattr(db, "save_cookie", fail_second)
    with pytest.raises(RuntimeError):
        apply_pending_news(path, tmp_path / "before.db", before)
    assert db.get_pending_cookie(conn, first.id) and db.get_pending_cookie(conn, second.id)
    assert conn.execute("SELECT count(*) FROM cookies").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["text", "review", "source", "source_url"])
def test_drift_stops_before_backup_or_mutation(database, tmp_path, change):
    path, conn = database
    cookie = draft(conn)
    expected = plan(conn)
    if change == "text":
        conn.execute("UPDATE pending_cookies SET summary='Edited' WHERE id=?", (cookie.id,))
    elif change == "review":
        conn.execute("UPDATE pending_cookies SET review_status='regenerating' WHERE id=?", (cookie.id,))
    elif change == "source":
        conn.execute("UPDATE sources SET item_type='judgment' WHERE id=?", (cookie.source_ids[0],))
    else:
        conn.execute("UPDATE sources SET source_url='https://example.org/changed' WHERE id=?", (cookie.source_ids[0],))
    conn.commit()
    backup = tmp_path / "before.db"
    with pytest.raises(NewsApprovalError, match="drift"):
        apply_pending_news(path, backup, expected)
    assert not backup.exists() and db.get_pending_cookie(conn, cookie.id)


def test_new_pending_not_silently_added_to_reviewed_plan(database, tmp_path):
    path, conn = database
    original = draft(conn)
    expected = plan(conn)
    later = draft(conn, headline="Later news")
    result = apply_pending_news(path, tmp_path / "before.db", expected)
    assert result["approved_ids"] == [original.id]
    assert db.get_pending_cookie(conn, later.id)


def test_existing_backup_is_never_overwritten(database, tmp_path):
    path, conn = database
    cookie = draft(conn)
    backup = tmp_path / "before.db"
    backup.write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        apply_pending_news(path, backup, plan(conn))
    assert backup.read_text() == "do not overwrite" and db.get_pending_cookie(conn, cookie.id)


def test_database_replacement_check_rolls_back(database, tmp_path, monkeypatch):
    from sg_law_cookies import news_approval

    path, conn = database
    cookie = draft(conn)
    expected = plan(conn)
    identity = news_approval._file_identity(path)
    actual_save = db.save_cookie
    changed = False
    def simulate_replace(*args, **kwargs):
        nonlocal changed
        result = actual_save(*args, **kwargs)
        changed = True
        return result
    monkeypatch.setattr(db, "save_cookie", simulate_replace)
    monkeypatch.setattr(news_approval, "_file_identity",
                        lambda p: (identity[0], identity[1]+1) if changed else identity)
    with pytest.raises(NewsApprovalError, match="database replaced"):
        apply_pending_news(path, tmp_path / "before.db", expected)
    assert db.get_cookie(conn, cookie.id) is None
    assert db.get_pending_cookie(conn, cookie.id)


def test_cli_dry_run_is_read_only_and_apply_requires_enabled_policy(database, tmp_path, monkeypatch, capsys):
    path, conn = database
    cookie = draft(conn)
    monkeypatch.setenv("COOKIES_DB_PATH", str(path))
    monkeypatch.setenv("COOKIES_AUTO_APPROVE_NEWS", "false")
    monkeypatch.setattr(db, "init_db", lambda *a: pytest.fail("dry-run must not initialise DB"))
    assert main(["approve-news"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["action"] == "dry-run" and report["eligible_ids"] == [cookie.id]
    assert main(["approve-news", "--apply"]) == 1
    assert "must be enabled" in capsys.readouterr().err
    ro = open_news_db(path)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM pending_cookies")
    ro.close()


def test_shared_72h_judgment_policy_unchanged(database):
    _, conn = database
    young = draft(conn, kind="judgment")
    old = draft(conn, kind="judgment", headline="Old judgment",
                created=datetime.now(timezone.utc)-timedelta(hours=80))
    assert db.auto_approve_pending(conn, max_age_hours=72) == [old.id]
    assert db.get_pending_cookie(conn, young.id)


def test_enabled_news_policy_excludes_review_leftovers_from_legacy_sweep(database):
    _, conn = database
    old_time = datetime.now(timezone.utc)-timedelta(hours=80)
    news = draft(conn, created=old_time, rejected=True)
    malformed = draft(conn, created=old_time, headline=" ")
    judgment = draft(conn, created=old_time, kind="judgment", headline="Judgment")
    assert db.auto_approve_pending(conn, exclude_news=True) == [judgment.id]
    assert db.get_pending_cookie(conn, news.id) and db.get_pending_cookie(conn, malformed.id)


def test_disabled_news_policy_preserves_legacy_72h_behavior(database):
    _, conn = database
    news = draft(conn, created=datetime.now(timezone.utc)-timedelta(hours=80))
    assert db.auto_approve_pending(conn) == [news.id]


@respx.mock
def test_duplicate_topics_within_one_future_extraction(database):
    _, conn = database
    mock_folio()
    with httpx.Client() as client:
        cookies = process_news(make_raw_item(), conn,
                               StubAnthropic([[topic_dict(), topic_dict()]]),
                               client, auto_approve_news=True)
    assert len(cookies) == 2 and cookies[1].is_duplicate
    assert cookies[1].duplicate_of == cookies[0].id


def test_cli_enabled_apply_requires_plan_and_backup_then_is_idempotent(database, tmp_path, monkeypatch, capsys):
    path, conn = database
    cookie = draft(conn)
    monkeypatch.setenv("COOKIES_DB_PATH", str(path))
    monkeypatch.setenv("COOKIES_AUTO_APPROVE_NEWS", "true")
    assert main(["approve-news", "--apply"]) == 1
    assert "requires --backup" in capsys.readouterr().err
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(plan(conn)))
    args = ["approve-news", "--apply", "--expected-plan", str(plan_file),
            "--backup", str(tmp_path / "before.db")]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["approved_ids"] == [cookie.id]
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["already_applied_ids"] == [cookie.id]
