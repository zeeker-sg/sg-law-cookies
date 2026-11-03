"""Private, copy-only migration/reconciliation tools. Never replace a live DB."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from sg_law_cookies import foundation


# Additive legacy columns introduced after the structural migration
# (approval provenance on `cookies`).  Legacy-equivalence comparisons
# project these out so exactly this sanctioned additive change — and
# nothing else — can differ between a baseline and a migrated copy.
# The full digest()/manifest() never projects anything; only callers
# comparing captured legacy state opt in via drop_columns.
ADDITIVE_LEGACY_COLUMNS = {"cookies": ("approval_method", "approved_by", "approved_at")}


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def manifest(path, *, legacy_only=False, drop_columns=None):
    """Exact logical row/column manifests, with no record contents in the result.

    drop_columns maps table -> column names to exclude from the manifest
    (used only for the sanctioned additive legacy columns; see
    ADDITIVE_LEGACY_COLUMNS).
    """
    conn = readonly(path)
    try:
        conn.execute("BEGIN")
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        result = {}
        for table in names:
            if legacy_only and table not in foundation.LEGACY_TABLES:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in conn.execute("PRAGMA table_info(" + quoted + ")")]
            drop = set((drop_columns or {}).get(table, ()))
            selected = [c for c in columns if c not in drop]
            select_sql = ", ".join('"' + c.replace('"', '""') + '"' for c in selected) or "NULL"
            rows = sorted(json.dumps([{"blob_hex": v.hex()} if isinstance(v, bytes) else v for v in r], ensure_ascii=False, separators=(",", ":")) for r in conn.execute("SELECT " + select_sql + " FROM " + quoted))
            encoded = json.dumps([selected, rows], ensure_ascii=False, separators=(",", ":")).encode()
            result[table] = {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest(), "columns": selected}
        return result
    finally:
        conn.close()


def digest(path):
    return hashlib.sha256(json.dumps(manifest(path), sort_keys=True).encode()).hexdigest()


def snapshot(source, target):
    """Exclusive private output; SQLite backup includes committed WAL data."""
    source, target = Path(source), Path(target)
    if source.resolve() == target.resolve():
        raise foundation.SchemaError("copy destination must differ from the source")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        src = readonly(source)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _assert_added_columns_carry_no_history(path, before):
    """Columns newly added by a migration must hold only honest defaults.

    approval_method must read 'unknown' everywhere and approved_by /
    approved_at must be NULL: pre-existing rows keep unknown history
    unknown, never an inferred or backfilled approval.
    """
    from sg_law_cookies import db

    if "cookies" not in before:
        return
    added = [c for c in ADDITIVE_LEGACY_COLUMNS["cookies"]
             if c not in before["cookies"]["columns"]]
    if not added:
        return
    conn = readonly(path)
    try:
        if "approval_method" in added:
            values = {r[0] for r in conn.execute("SELECT DISTINCT approval_method FROM cookies")}
            if values - {db.APPROVAL_METHOD_UNKNOWN}:
                raise foundation.SchemaError("migration invented approval history")
        for column in ("approved_by", "approved_at"):
            if column in added and conn.execute(
                f"SELECT 1 FROM cookies WHERE {column} IS NOT NULL LIMIT 1"
            ).fetchone():
                raise foundation.SchemaError("migration invented approval history")
    finally:
        conn.close()


def migrate_copy(source, target):
    from sg_law_cookies import db

    snapshot(source, target)
    before = manifest(target, legacy_only=True, drop_columns=ADDITIVE_LEGACY_COLUMNS)
    conn = db.init_db(target, migrate=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise foundation.SchemaError("migration integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise foundation.SchemaError("migration foreign key check failed")
        # init_db performed the migration; a second invocation must be a no-op.
        if foundation.migrate(conn):
            raise foundation.SchemaError("repeat migration was not a no-op")
    finally:
        conn.close()
    after = manifest(target, legacy_only=True, drop_columns=ADDITIVE_LEGACY_COLUMNS)
    if before != after:
        raise foundation.SchemaError("migration changed the legacy record/column sets")
    _assert_added_columns_carry_no_history(target, before)
    return {"legacy_equivalent": True, "repeat_migration_noop": True,
            "legacy_tables": after, "migrated_digest": digest(target)}


def prepare_rollback(current, baseline, output, *, expected_current_digest):
    """Produce a rollback candidate only if no writes occurred since rehearsal.

    This does not replace files or stop writers. Production replacement requires
    a separately approved, externally enforced writer freeze and reconciliation.
    """
    if digest(current) != expected_current_digest:
        raise foundation.SchemaError("intervening writes: rollback requires reconciliation, not snapshot replacement")
    if manifest(current, legacy_only=True, drop_columns=ADDITIVE_LEGACY_COLUMNS) != manifest(baseline, legacy_only=True, drop_columns=ADDITIVE_LEGACY_COLUMNS):
        raise foundation.SchemaError("rollback baseline differs from captured legacy state")
    snapshot(baseline, output)


def validate_restore(candidate, current=None):
    """Validate a downloaded canonical DB without replacing local state.

    A running host only accepts an identical snapshot (a no-op). Changed state
    requires an explicit offline reconciliation; file replacement cannot fence
    an already-open reviewer or ingestion connection.
    """
    candidate = Path(candidate)
    conn = readonly(candidate)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise foundation.SchemaError("restore integrity check failed")
        foundation.validate_contract(conn)
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise foundation.SchemaError("restore foreign key check failed")
    finally:
        conn.close()
    if current is not None and Path(current).exists():
        if digest(current) != digest(candidate):
            raise foundation.SchemaError("canonical/local state differs; stop writers and reconcile before offline restore")
        return False
    return True
