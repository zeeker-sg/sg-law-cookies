#!/usr/bin/env python3
"""Offline rehearsal on private copies; no network, models, publishing or live writes."""

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sg_law_cookies import db, foundation
from sg_law_cookies.migration import digest, manifest, migrate_copy, prepare_rollback, readonly, snapshot, validate_restore
from sg_law_cookies.public_export import export_public_db
from sg_law_cookies.sitegen import build_site


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--base-url", default="https://cookies.zeeker.sg")
    args = parser.parse_args()
    os.umask(0o077)
    args.out.mkdir(mode=0o700)  # Existing output is an error, never an overwrite.
    baseline = args.out / "baseline.db"
    snapshot(args.source, baseline)
    migrated = args.out / "migrated.db"
    report = migrate_copy(baseline, migrated)
    conn = db.init_db(migrated)
    report["foundation_counts"] = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        for t in ("stories", "insights", "documents", "input_observations", "presentation_revisions", "legacy_identities", "review_events", "relationship_assessments", "processing_receipts")}
    report["legacy_unknowns_preserved"] = conn.execute("SELECT count(*) FROM input_observations WHERE input_kind IS NOT NULL OR input_projection IS NOT NULL OR date_basis IS NOT NULL").fetchone()[0] == 0
    conn.close()
    export_clock = datetime.now(timezone.utc)
    report["fixed_export_clock_utc"] = export_clock.isoformat()
    for name, path in (("before", baseline), ("after", migrated)):
        conn = readonly(path)
        try:
            build_site(conn, args.out / name, args.base_url)
        finally:
            conn.close()
        export_public_db(path, args.out / (name + "-public.db"), upload=False, now=export_clock)
    report["public_database_byte_identical"] = (args.out / "before-public.db").read_bytes() == (args.out / "after-public.db").read_bytes()
    report["public_table_equivalence"] = manifest(args.out / "before-public.db") == manifest(args.out / "after-public.db")
    before_files, after_files = files(args.out / "before"), files(args.out / "after")
    report["rendered_files"] = len(before_files)
    report["rendered_files_byte_identical"] = before_files == after_files
    report["public_tables"] = sorted(manifest(args.out / "after-public.db"))
    assert report["public_table_equivalence"] and report["rendered_files_byte_identical"]
    assert report["legacy_unknowns_preserved"]
    prepare_rollback(migrated, baseline, args.out / "rollback.db", expected_current_digest=report["migrated_digest"])
    report["rollback_copy_equivalent"] = manifest(args.out / "rollback.db") == manifest(baseline)
    assert validate_restore(migrated, migrated) is False
    mutated = args.out / "writer-exercises.db"
    snapshot(migrated, mutated)
    conn = db.init_db(mutated)
    try:
        pending = db.list_pending_cookies(conn)
        if pending:
            row = pending[0]
            db.update_pending_cookie_text(conn, row["id"], summary=row["summary"] + " [private rehearsal]", expected_fingerprint=db.pending_fingerprint(row))
            db.promote_pending_cookie(conn, row["id"], expected_fingerprint=db.pending_fingerprint(db.get_pending_cookie(conn, row["id"])))
        item = db.all_cookies(conn)[0]
        stale = item.model_copy(deep=True)
        item.summary += " [private rehearsal]"
        db.save_cookie(conn, item)
        try:
            db.save_cookie(conn, stale)
            raise AssertionError("stale save was accepted")
        except foundation.StaleRevisionError:
            pass
        src = db.get_source(conn, item.source_ids[0])
        old_count = conn.execute("SELECT count(*) FROM input_observations WHERE legacy_source_id=?", (src.id,)).fetchone()[0]
        src.raw_text += " [private enrichment rehearsal]"
        src.input_provenance = {"input_kind": "other", "selected_field": "rehearsal_only"}
        db.upsert_source(conn, src)
        assert conn.execute("SELECT count(*) FROM input_observations WHERE legacy_source_id=?", (src.id,)).fetchone()[0] == old_count + 1
        db.record_processing(conn, src.id, "rehearsal", "no-model", [item.id])
        assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    try:
        prepare_rollback(mutated, baseline, args.out / "unsafe-rollback.db", expected_current_digest=report["migrated_digest"])
        raise AssertionError("unsafe rollback was accepted")
    except foundation.SchemaError:
        report["intervening_write_rollback_blocked"] = True
    try:
        validate_restore(migrated, mutated)
        raise AssertionError("divergent restore was accepted")
    except foundation.SchemaError:
        report["divergent_restore_blocked"] = True
    report["writer_exercises_passed"] = True
    report["baseline_sha256"] = hashlib.sha256(baseline.read_bytes()).hexdigest()
    report["no_network_or_model_calls"] = True
    report["source_baseline_unchanged"] = manifest(baseline, legacy_only=True) == report["legacy_tables"]
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
