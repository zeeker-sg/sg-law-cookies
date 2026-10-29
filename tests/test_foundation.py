"""Structural migration and writer-boundary contracts, without live APIs."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from sg_law_cookies import db, foundation
from sg_law_cookies.migration import (
    digest, manifest, migrate_copy, prepare_rollback, snapshot, validate_restore,
)
from sg_law_cookies.models import Cookie, Source


def source(**changes):
    values = dict(source_url="https://example.test/document", zeeker_url="https://data.test/db/table/1",
                  title="Document", raw_text="PRIVATE ORIGINAL EVIDENCE", date=date(2026, 6, 1),
                  source_id="test", item_type="news", license="reserved", token_count=4)
    values.update(changes)
    return Source(**values)


def cookie(src, **changes):
    values = dict(source_ids=[src.id], headline="A legal development", summary="Public account",
                  why_it_matters="A legal consequence", significance="medium")
    values.update(changes)
    return Cookie(**values)


@pytest.fixture
def live(tmp_path):
    conn = db.init_db(tmp_path / "current.db")
    src = source()
    db.upsert_source(conn, src)
    item = cookie(src)
    db.save_cookie(conn, item)
    item = db.get_cookie(conn, item.id)
    yield conn, src, item
    conn.close()


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript(db._SCHEMA)
    conn.execute("ALTER TABLE cookies ADD COLUMN admitted_at TEXT")
    conn.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 ("s1", "https://example.test/a", "https://data.test/db/table/1", "Original", "PRIVATE LEGACY INPUT", "2026-01-01", "db", "news", "reserved", 5, "2026-06-01T00:00:00+00:00"))
    conn.execute("INSERT INTO cookies VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("c1", "Headline", "Summary", "Why", "high", '[ {"iri": null} ]', "[]", "[]", '["unknown"]', 0, None, "2026-06-01T23:30:00-02:00", "2026-06-01T23:30:00-02:00"))
    conn.execute("INSERT INTO cookie_sources VALUES ('c1','s1')")
    conn.execute("INSERT INTO pending_cookies(id,headline,summary,why_it_matters,significance,source_ids,item_type,created_at,discord_msg_id,reject_count,reject_reason) VALUES ('p1','Draft','Text','Why','low','[\"s1\"]','judgment','2026-06-02T00:00:00+00:00','discord-1',1,'Earlier feedback')")
    conn.execute("INSERT INTO rejected_cookies VALUES ('r1','Rejected','Text','Why','low','[]','[]','[]','[]','[\"s1\"]','news','u',2,'Final reason','reviewer','2026-06-03','2026-06-01')")
    conn.commit()
    conn.close()
    return path


def test_migration_is_mechanical_complete_and_repeatable(legacy, tmp_path):
    target = tmp_path / "migrated.db"
    before = manifest(legacy)
    report = migrate_copy(legacy, target)
    assert report["legacy_equivalent"] and report["repeat_migration_noop"]
    assert manifest(target, legacy_only=True) == before
    conn = db.init_db(target)
    assert conn.execute("SELECT count(*) FROM stories").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM insights").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM relationship_assessments").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM processing_receipts").fetchone()[0] == 0
    obs = conn.execute("SELECT * FROM input_observations").fetchone()
    assert obs["input_kind"] is None and obs["input_projection"] is None
    assert obs["date_basis"] is None and obs["upstream_enriched_at"] is None
    assert obs["raw_text"] == "PRIVATE LEGACY INPUT"
    revision = conn.execute("SELECT * FROM presentation_revisions").fetchone()
    assert revision["capture_kind"] == "migration_capture"
    assert revision["approved_at"] is None
    identity = conn.execute("SELECT * FROM legacy_identities").fetchone()
    assert identity["cookie_id"] == "c1"
    assert identity["atom_day"] == "2026-06-01"
    assert identity["daily_day"] == "2026-06-02"
    assert conn.execute("SELECT operation FROM review_events").fetchone()[0] == "migration_capture"
    captured = digest(target)
    assert foundation.migrate(conn) is False
    conn.close()
    assert digest(target) == captured


def test_legacy_requires_explicit_migration(legacy):
    with pytest.raises(foundation.SchemaError, match="explicit"):
        db.init_db(legacy)


def test_unknown_admission_is_not_invented_by_capture(legacy, tmp_path):
    old = sqlite3.connect(legacy)
    old.execute("UPDATE cookies SET admitted_at=NULL")
    old.commit()
    old.close()
    target = tmp_path / "unknown-date.db"
    migrate_copy(legacy, target)
    conn = db.init_db(target)
    identity = conn.execute("SELECT * FROM legacy_identities").fetchone()
    assert identity["recorded_admitted_at"] is None and identity["daily_day"] is None
    # Atom's existing literal-ID fallback is an identity rule, not an admission claim.
    assert identity["atom_day"] == "2026-06-01"
    story = conn.execute("SELECT * FROM stories").fetchone()
    assert story["first_admitted_at_recorded"] is None
    assert story["state"] == "legacy_captured"
    conn.close()


def test_copy_migration_cannot_overwrite_source_or_existing_output(legacy, tmp_path):
    with pytest.raises(foundation.SchemaError):
        migrate_copy(legacy, legacy)
    existing = tmp_path / "existing"
    existing.write_text("keep")
    with pytest.raises(FileExistsError):
        migrate_copy(legacy, existing)
    assert existing.read_text() == "keep"


def test_copy_migration_cli_uses_explicit_paths(legacy, tmp_path, monkeypatch, capsys):
    from sg_law_cookies import cli
    monkeypatch.setattr(cli, "load_settings", lambda: object())
    before = digest(legacy)
    output = tmp_path / "cli-copy.db"
    assert cli.main(["migrate-schema", "--source", str(legacy), "--out", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["legacy_equivalent"]
    assert digest(legacy) == before
    assert manifest(output, legacy_only=True) == manifest(legacy)


def test_enrichment_retains_original_observation_and_evidence(live):
    conn, src, item = live
    old_evidence = conn.execute("SELECT observation_id FROM insight_evidence").fetchone()[0]
    enriched = src.model_copy(update={"raw_text": "Richer evidence", "input_provenance": {
        "input_kind": "full_text", "selected_field": "content_text", "input_projection": "datasette_default_json"}})
    db.upsert_source(conn, enriched)
    assert conn.execute("SELECT count(*) FROM input_observations").fetchone()[0] == 2
    assert conn.execute("SELECT raw_text FROM input_observations WHERE id=?", (old_evidence,)).fetchone()[0] == src.raw_text
    assert conn.execute("SELECT observation_id FROM insight_evidence").fetchone()[0] == old_evidence
    assert foundation.revision(conn, item.id) == 1
    assert conn.execute("SELECT input_kind FROM input_observations WHERE raw_text='Richer evidence'").fetchone()[0] == "full_text"


def test_text_revision_preserves_union_and_rejects_stale_save(live):
    conn, src, item = live
    stale = db.get_cookie(conn, item.id)
    other = source(source_url="https://example.test/b")
    db.upsert_source(conn, other)
    db.link_cookie_source(conn, item.id, other.id)
    stale.summary = "Stale replacement"
    with pytest.raises(foundation.StaleRevisionError):
        db.save_cookie(conn, stale)
    fresh = db.get_cookie(conn, item.id)
    fresh.source_ids = [src.id]  # A partial caller cannot erase corroboration.
    fresh.summary = "Reviewed revision"
    db.save_cookie(conn, fresh)
    current = db.get_cookie(conn, item.id)
    assert set(current.source_ids) == {src.id, other.id}
    assert current.summary == "Reviewed revision"
    assert current.admitted_at == item.admitted_at
    assert conn.execute("SELECT meaningful_update_at FROM stories").fetchone()[0] is None


def test_cross_connection_stale_token(live):
    conn, _, item = live
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    other = db.init_db(path)
    try:
        stale = db.get_cookie(other, item.id)
        fresh = db.get_cookie(conn, item.id)
        fresh.headline = "Updated"
        db.save_cookie(conn, fresh)
        stale.headline = "Lost update"
        with pytest.raises(foundation.StaleRevisionError):
            db.save_cookie(other, stale)
    finally:
        other.close()


def test_failed_revision_rolls_back_projection_history_and_links(live):
    conn, _, item = live
    before = digest(conn.execute("PRAGMA database_list").fetchone()[2])
    fresh = db.get_cookie(conn, item.id)
    fresh.summary = "Must roll back"
    fresh.source_ids.append("missing-source")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_cookie(conn, fresh)
    assert digest(conn.execute("PRAGMA database_list").fetchone()[2]) == before


def test_direct_new_writer_flushes_but_old_writer_is_blocked(live):
    conn, _, item = live
    conn.execute("UPDATE cookies SET summary='Audited SQL edit' WHERE id=?", (item.id,))
    conn.commit()
    head = conn.execute("SELECT payload_json FROM presentation_revisions ORDER BY revision DESC LIMIT 1").fetchone()[0]
    assert json.loads(head)["summary"] == "Audited SQL edit"
    old = sqlite3.connect(conn.execute("PRAGMA database_list").fetchone()[2])
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such function: cookies_"):
            old.execute("UPDATE cookies SET summary='Old writer' WHERE id=?", (item.id,))
        old.rollback()
        assert old.execute("SELECT count(*) FROM cookies").fetchone()[0] == 1
    finally:
        old.close()


@pytest.mark.parametrize("table", ["input_observations", "insight_versions", "presentation_revisions", "write_events"])
def test_history_is_append_only(live, table):
    conn, _, _ = live
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"DELETE FROM {table}")
    conn.rollback()


def test_context_manager_flushes_atomically(live):
    conn, _, item = live
    with conn:
        conn.execute("UPDATE cookies SET summary='Context edit' WHERE id=?", (item.id,))
    assert foundation.revision(conn, item.id) == 2
    with pytest.raises(ValueError):
        with conn:
            conn.execute("UPDATE cookies SET summary='Failure' WHERE id=?", (item.id,))
            raise ValueError()
    assert db.get_cookie(conn, item.id).summary == "Context edit"


def test_draft_edit_and_regeneration_history_stale_guard(live):
    conn, src, _ = live
    draft = cookie(src)
    db.save_pending_cookie(conn, draft, "judgment")
    before = db.get_pending_cookie(conn, draft.id)
    token = db.pending_fingerprint(before)
    db.set_pending_discord_msg_id(conn, draft.id, "message")
    assert db.pending_fingerprint(db.get_pending_cookie(conn, draft.id)) == token
    db.update_pending_cookie_text(conn, draft.id, summary="Changed", expected_fingerprint=token)
    with pytest.raises(foundation.StaleRevisionError):
        db.update_pending_cookie_text(conn, draft.id, summary="Stale", expected_fingerprint=token)
    assert conn.execute("SELECT count(*) FROM review_events WHERE cookie_id=?", (draft.id,)).fetchone()[0] == 3
    row = db.get_pending_cookie(conn, draft.id)
    db.queue_pending_regeneration(conn, draft.id, "Feedback", expected_fingerprint=db.pending_fingerprint(row))
    row = db.get_pending_cookie(conn, draft.id)
    new = cookie(src)
    with pytest.raises(foundation.StaleRevisionError):
        with db.write_transaction(conn):
            db.save_pending_cookie(conn, new, "judgment")
            db.finish_regeneration(conn, draft.id, expected_fingerprint="stale")
    assert db.get_pending_cookie(conn, new.id) is None
    with db.write_transaction(conn):
        db.save_pending_cookie(conn, new, "judgment")
        db.finish_regeneration(conn, draft.id, expected_fingerprint=db.pending_fingerprint(row))
        db.record_processing(conn, src.id, "regeneration", "test", [new.id])
    assert db.get_pending_cookie(conn, draft.id) is None
    assert db.get_pending_cookie(conn, new.id) is not None


def test_approval_preserves_identity_and_rejection_history(live):
    conn, src, _ = live
    draft = cookie(src)
    db.save_pending_cookie(conn, draft, "judgment")
    row = db.get_pending_cookie(conn, draft.id)
    promoted = db.promote_pending_cookie(conn, draft.id, expected_fingerprint=db.pending_fingerprint(row))
    assert promoted.id == draft.id
    assert db.get_pending_cookie(conn, draft.id) is None
    assert foundation.revision(conn, draft.id) == 1
    other = cookie(src)
    db.save_pending_cookie(conn, other, "judgment")
    assert db.reject_pending_cookie(conn, other.id, "First") == "regenerating"
    assert db.reject_pending_cookie(conn, other.id, "Second") == "rejected"
    assert db.list_rejected_cookies(conn)[0]["reject_reason"] == "Second"
    events = conn.execute("SELECT before_json FROM review_events WHERE cookie_id=? AND operation='DELETE'", (other.id,)).fetchone()
    assert json.loads(events[0])["reject_reason"] == "First"


def test_processing_receipt_is_atomic_and_idempotent(live):
    conn, src, item = live
    db.record_processing(conn, src.id, "news", "model+config", [item.id])
    db.record_processing(conn, src.id, "news", "model+config", [item.id])
    assert conn.execute("SELECT count(*) FROM processing_receipts").fetchone()[0] == 1
    assert foundation.processing_complete(conn, src.id)


def test_failed_nested_source_write_does_not_poison_provenance(live):
    conn, src, _ = live
    good = src.model_copy(update={"raw_text": "Full input", "input_provenance": {"input_kind": "full_text"}})
    bad = src.model_copy(update={"item_type": "invalid", "input_provenance": {"input_kind": "generated_summary"}})
    with db.write_transaction(conn):
        db.upsert_source(conn, good)
        with pytest.raises(sqlite3.IntegrityError):
            db.upsert_source(conn, bad)
    assert conn.execute("SELECT input_kind FROM input_observations WHERE raw_text='Full input'").fetchone()[0] == "full_text"


def test_actual_backup_cold_restore_noop_and_divergence(live, tmp_path, monkeypatch):
    from sg_law_cookies.backup import backup_db, restore_db, BackupError

    class MemoryS3:
        def __init__(self):
            self.files = {}

        def upload_file(self, filename, bucket, key):
            self.files[key] = Path(filename).read_bytes()

        def download_file(self, bucket, key, filename):
            Path(filename).write_bytes(self.files[key])

    conn, _, item = live
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    client = MemoryS3()
    backup_db(Path(path), bucket="private-test", s3_client=client)
    cold = tmp_path / "cold.db"
    result = restore_db(cold, bucket="private-test", s3_client=client)
    assert not result.unchanged
    assert manifest(cold) == manifest(path)
    import sg_law_cookies.backup as backup
    monkeypatch.setattr(backup.shutil, "move", lambda *a: pytest.fail("running DB must not be replaced"))
    assert restore_db(Path(path), bucket="private-test", s3_client=client).unchanged
    fresh = db.get_cookie(conn, item.id)
    fresh.summary = "Keep this intervening edit"
    db.save_cookie(conn, fresh)
    before = digest(path)
    with pytest.raises(BackupError, match="reconcile"):
        restore_db(Path(path), bucket="private-test", s3_client=client)
    assert digest(path) == before


def test_backup_rejects_legacy_before_upload(legacy):
    from sg_law_cookies.backup import backup_db, BackupError

    class NoUpload:
        def upload_file(self, *args):
            pytest.fail("unsupported schema must not replace canonical backup")

    with pytest.raises(BackupError, match="unsupported"):
        backup_db(legacy, bucket="private-test", s3_client=NoUpload())


def test_invalid_restore_does_not_replace_local_state(live):
    from pathlib import Path
    from sg_law_cookies.backup import restore_db, BackupError

    class InvalidS3:
        def download_file(self, bucket, key, filename):
            Path(filename).write_bytes(b"not a database")

    conn, _, _ = live
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    before = digest(path)
    with pytest.raises(BackupError):
        restore_db(path, bucket="private-test", s3_client=InvalidS3())
    assert digest(path) == before


def test_rollback_candidate_rejects_intervening_writes(legacy, tmp_path):
    target = tmp_path / "new.db"
    report = migrate_copy(legacy, target)
    rollback = tmp_path / "rollback.db"
    prepare_rollback(target, legacy, rollback, expected_current_digest=report["migrated_digest"])
    assert manifest(rollback) == manifest(legacy)
    conn = db.init_db(target)
    db.set_pending_discord_msg_id(conn, "p1", "new-message")
    conn.close()
    with pytest.raises(foundation.SchemaError, match="intervening writes"):
        prepare_rollback(target, legacy, tmp_path / "unsafe.db", expected_current_digest=report["migrated_digest"])
    assert not (tmp_path / "unsafe.db").exists()


def test_restore_validates_schema_integrity_and_divergence(live, legacy, tmp_path):
    conn, _, item = live
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    snap = tmp_path / "backup.db"
    snapshot(path, snap)
    assert validate_restore(snap, path) is False
    assert validate_restore(snap) is True
    with pytest.raises(foundation.SchemaError, match="unsupported"):
        validate_restore(legacy, path)
    fresh = db.get_cookie(conn, item.id)
    fresh.summary = "Intervening approval/edit"
    db.save_cookie(conn, fresh)
    with pytest.raises(foundation.SchemaError, match="reconcile"):
        validate_restore(snap, path)
    assert db.get_cookie(conn, item.id).summary == "Intervening approval/edit"


def test_future_schema_is_rejected_before_init(live):
    conn, _, _ = live
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.execute("UPDATE foundation_state SET schema_version=999")
    # Deliberate fixture corruption through base commit; production APIs cannot
    # interpret this future version and must fail before changing the schema.
    sqlite3.Connection.commit(conn)
    with pytest.raises(foundation.SchemaError):
        db.init_db(path)
