"""Private, copy-only migration/reconciliation tools. Never replace a live DB."""

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from sg_law_cookies import foundation


def readonly(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def manifest(path, *, legacy_only=False):
    """Exact logical row/column manifests, with no record contents in the result."""
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
            rows = sorted(json.dumps([{"blob_hex": v.hex()} if isinstance(v, bytes) else v for v in r], ensure_ascii=False, separators=(",", ":")) for r in conn.execute("SELECT * FROM " + quoted))
            encoded = json.dumps([columns, rows], ensure_ascii=False, separators=(",", ":")).encode()
            result[table] = {"rows": len(rows), "sha256": hashlib.sha256(encoded).hexdigest(), "columns": columns}
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


def migrate_copy(source, target):
    from sg_law_cookies import db

    snapshot(source, target)
    before = manifest(target, legacy_only=True)
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
    after = manifest(target, legacy_only=True)
    if before != after:
        raise foundation.SchemaError("migration changed the legacy record/column sets")
    return {"legacy_equivalent": True, "repeat_migration_noop": True,
            "legacy_tables": after, "migrated_digest": digest(target)}


def prepare_rollback(current, baseline, output, *, expected_current_digest):
    """Produce a rollback candidate only if no writes occurred since rehearsal.

    This does not replace files or stop writers. Production replacement requires
    a separately approved, externally enforced writer freeze and reconciliation.
    """
    if digest(current) != expected_current_digest:
        raise foundation.SchemaError("intervening writes: rollback requires reconciliation, not snapshot replacement")
    if manifest(current, legacy_only=True) != manifest(baseline, legacy_only=True):
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
