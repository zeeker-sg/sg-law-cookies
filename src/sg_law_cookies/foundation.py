"""Versioned structural foundation behind the unchanged legacy projection.

No semantic grouping happens here. A migration capture is evidence of the
captured database state, never a reconstruction of original publication.
"""

import functools
import hashlib
import inspect
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid4, uuid5

SCHEMA_VERSION = 1
LEGACY_TABLES = (
    "sources", "cookies", "cookie_sources", "pending_cookies", "rejected_cookies",
    "judgment_meta", "source_registry", "daily_stats", "unresolved_terms",
)
TEXT_FIELDS = ("headline", "summary", "why_it_matters", "significance",
               "folio_areas", "folio_entities", "folio_concepts", "unresolved")


class SchemaError(RuntimeError):
    pass


class StaleRevisionError(SchemaError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _id(kind, legacy):
    return str(uuid5(NAMESPACE_URL, f"sg-law-cookies/{kind}/{legacy}"))


def schema_version(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='foundation_state'").fetchone():
        return 0
    return conn.execute("SELECT schema_version FROM foundation_state WHERE singleton=1").fetchone()[0]


def require_schema(conn):
    if schema_version(conn) != SCHEMA_VERSION:
        raise SchemaError("unsupported schema; run the explicit structural migration before writing")


def validate_contract(conn):
    require_schema(conn)
    expected = {"foundation_state", "documents", "document_identifiers", "input_observations",
                "source_observation_heads", "stories", "insights", "insight_versions", "insight_evidence",
                "presentation_revisions", "legacy_identities", "relationship_assessments", "processing_receipts",
                "review_events", "write_events", "foundation_dirty", *LEGACY_TABLES}
    actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not expected <= actual:
        raise SchemaError("incomplete structural schema")
    triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    required = {f"guard_{table}_{op}" for table in LEGACY_TABLES for op in ("INSERT", "UPDATE", "DELETE")}
    if not required <= triggers or conn.execute("SELECT 1 FROM foundation_dirty LIMIT 1").fetchone():
        raise SchemaError("missing writer guards or uncommitted structural projection")


class Connection(sqlite3.Connection):
    """Flush complete structural snapshots in the same SQLite commit as legacy writes.

    Guard triggers reject old runtimes and ordinary sqlite3 writers. Dirty queues
    also cover audited direct SQL in the new runtime; a commit cannot leave the
    compatibility projection ahead of its revisions.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._atomic_depth = 0
        self._txid = None
        self._migrating = False
        self._provenance = {}
        self._contexts = []
        self.create_function("cookies_writer_version", 0, lambda: SCHEMA_VERSION)
        self.create_function("cookies_transaction_id", 0, self.transaction_id)
        self.create_function("cookies_now", 0, _now)

    def transaction_id(self):
        if self._txid is None:
            self._txid = str(uuid4())
        return self._txid

    def commit(self):
        if self._atomic_depth:
            return
        try:
            if not self._migrating and schema_version(self) == SCHEMA_VERSION:
                flush(self)
            super().commit()
        except Exception:
            super().rollback()
            raise
        finally:
            self._txid = None
            self._provenance.clear()

    def rollback(self):
        super().rollback()
        self._txid = None
        self._provenance.clear()

    def __enter__(self):
        context = transaction(self)
        self._contexts.append(context)
        return context.__enter__()

    def __exit__(self, *exc):
        return self._contexts.pop().__exit__(*exc)

    def executescript(self, sql):
        if not self._migrating:
            raise SchemaError("executescript bypasses revision commits; use a write transaction")
        return super().executescript(sql)


@contextmanager
def transaction(conn):
    """Nested savepoints; one outer commit, including revision/evidence snapshots."""
    if not isinstance(conn, Connection):
        raise SchemaError("writers must use the version-aware database connection")
    require_schema(conn)
    outer = conn._atomic_depth == 0
    owned = outer and not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    savepoint = "foundation_" + uuid4().hex
    provenance_before = dict(conn._provenance)
    conn.execute(f"SAVEPOINT {savepoint}")
    conn._atomic_depth += 1
    try:
        yield conn
        conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        conn._provenance = provenance_before
        if owned:
            conn.rollback()
        raise
    finally:
        conn._atomic_depth -= 1
    if owned:
        conn.commit()


def atomic(fn):
    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        conn = bound.arguments["conn"]
        # commit=False is used only inside an explicitly owned batch.
        if bound.arguments.get("commit") is False and not conn.in_transaction:
            raise SchemaError("commit=False requires an outer transaction")
        with transaction(conn):
            result = fn(*args, **kwargs)
        cookie = bound.arguments.get("cookie")
        if cookie is not None and hasattr(cookie, "revision") and not conn._atomic_depth:
            cookie.revision = revision(conn, cookie.id)
        return result
    return wrapped


_DDL = """
CREATE TABLE foundation_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
 database_id TEXT NOT NULL, migrated_at TEXT NOT NULL
);
CREATE TABLE documents (
 id TEXT PRIMARY KEY, legacy_source_id TEXT UNIQUE NOT NULL REFERENCES sources(id),
 collector_namespace TEXT NOT NULL, recorded_upstream_url TEXT NOT NULL,
 original_url TEXT NOT NULL, recorded_title TEXT NOT NULL,
 recorded_legacy_date TEXT NOT NULL, publication_date TEXT, decision_date TEXT,
 document_role TEXT, date_basis TEXT, captured_at TEXT NOT NULL
);
CREATE TABLE document_identifiers (
 document_id TEXT NOT NULL REFERENCES documents(id), namespace TEXT NOT NULL,
 value TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('recorded','proposed','verified')),
 PRIMARY KEY(document_id,namespace,value)
);
CREATE TABLE input_observations (
 id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
 legacy_source_id TEXT NOT NULL REFERENCES sources(id), payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL, raw_text TEXT NOT NULL, text_sha256 TEXT NOT NULL,
 input_kind TEXT CHECK(input_kind IN ('full_text','court_summary','publisher_summary','generated_summary','rss_abstract','other')),
 selected_field TEXT, input_projection TEXT, upstream_version TEXT,
 date_basis TEXT, upstream_discovered_at TEXT, upstream_enriched_at TEXT,
 observed_at TEXT NOT NULL, captured_at TEXT NOT NULL, capture_kind TEXT NOT NULL,
 UNIQUE(legacy_source_id,payload_sha256)
);
CREATE TABLE source_observation_heads (
 source_id TEXT PRIMARY KEY REFERENCES sources(id), observation_id TEXT NOT NULL REFERENCES input_observations(id)
);
CREATE TABLE stories (
 id TEXT PRIMARY KEY, provisional INTEGER NOT NULL CHECK(provisional IN (0,1)),
 current_revision_id TEXT REFERENCES presentation_revisions(id) DEFERRABLE INITIALLY DEFERRED,
 first_admitted_at_recorded TEXT, meaningful_update_at TEXT, event_date TEXT,
 event_date_basis TEXT, state TEXT NOT NULL CHECK(state IN ('legacy_admitted','legacy_captured','admitted'))
);
CREATE TABLE insights (
 id TEXT PRIMARY KEY REFERENCES cookies(id), story_id TEXT NOT NULL REFERENCES stories(id),
 current_version_id TEXT REFERENCES insight_versions(id) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE insight_versions (
 id TEXT PRIMARY KEY, insight_id TEXT NOT NULL REFERENCES insights(id), version INTEGER NOT NULL,
 payload_json TEXT NOT NULL, captured_at TEXT NOT NULL, capture_kind TEXT NOT NULL,
 extraction_at_recorded TEXT, transaction_id TEXT NOT NULL, UNIQUE(insight_id,version)
);
CREATE TABLE insight_evidence (
 insight_version_id TEXT NOT NULL REFERENCES insight_versions(id),
 observation_id TEXT NOT NULL REFERENCES input_observations(id),
 role TEXT NOT NULL CHECK(role IN ('legacy_association','extraction_input','associated')),
 locator TEXT, PRIMARY KEY(insight_version_id,observation_id)
);
CREATE TABLE presentation_revisions (
 id TEXT PRIMARY KEY, story_id TEXT NOT NULL REFERENCES stories(id), revision INTEGER NOT NULL,
 insight_version_ids TEXT NOT NULL, payload_json TEXT NOT NULL, source_ids TEXT NOT NULL,
 captured_at TEXT NOT NULL, capture_kind TEXT NOT NULL, approval_basis TEXT NOT NULL,
 approved_at TEXT, decision_id TEXT, transaction_id TEXT NOT NULL, UNIQUE(story_id,revision)
);
CREATE TABLE legacy_identities (
 cookie_id TEXT PRIMARY KEY REFERENCES cookies(id), story_id TEXT NOT NULL REFERENCES stories(id),
 insight_id TEXT NOT NULL REFERENCES insights(id), recorded_created_at TEXT NOT NULL,
 recorded_admitted_at TEXT, atom_day TEXT NOT NULL, daily_day TEXT, sky_id TEXT NOT NULL
);
CREATE TABLE relationship_assessments (
 id TEXT PRIMARY KEY, layer TEXT NOT NULL CHECK(layer IN ('document','story','insight')),
 left_id TEXT NOT NULL, right_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('equivalent_document','url_alias','derived_from','syndicated_from','same_matter','later_development','correction','refines','supersedes')),
 status TEXT NOT NULL CHECK(status IN ('proposed','verified','rejected','reversed')),
 evidence_note TEXT NOT NULL, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,
 reverses_id TEXT REFERENCES relationship_assessments(id),
 CHECK(left_id != right_id),
 CHECK((layer='document' AND kind IN ('equivalent_document','url_alias','derived_from','syndicated_from'))
 OR (layer IN ('story','insight') AND kind IN ('same_matter','later_development','correction','refines','supersedes')))
);
CREATE TABLE processing_receipts (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
 observation_id TEXT NOT NULL REFERENCES input_observations(id), pipeline TEXT NOT NULL,
 extractor_version TEXT NOT NULL, output_ids TEXT NOT NULL, completed_at TEXT NOT NULL,
 UNIQUE(observation_id,pipeline,extractor_version,output_ids)
);
CREATE TABLE review_events (
 id INTEGER PRIMARY KEY, cookie_id TEXT NOT NULL, operation TEXT NOT NULL,
 before_json TEXT, after_json TEXT, captured_at TEXT NOT NULL, transaction_id TEXT NOT NULL
);
CREATE TABLE write_events (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL, table_name TEXT NOT NULL,
 operation TEXT NOT NULL, row_key TEXT NOT NULL, before_json TEXT, after_json TEXT,
 captured_at TEXT NOT NULL
);
CREATE TABLE foundation_dirty (kind TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(kind,id));
"""


def _exec_ddl(conn, sql):
    # executescript commits an existing transaction; never use it for migration.
    for statement in sql.split(";"):
        if statement.strip():
            conn.execute(statement)


def _row_json(alias, columns):
    return "json_object(" + ",".join(f"'{c}',{alias}.\"{c}\"" for c in columns) + ")"


def _install_triggers(conn):
    for table in LEGACY_TABLES:
        columns = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        for operation in ("INSERT", "UPDATE", "DELETE"):
            alias = "OLD" if operation == "DELETE" else "NEW"
            rowkey = f"{alias}.cookie_id || ':' || {alias}.source_id" if table == "cookie_sources" else (
                f"{alias}.zeeker_db || '/' || {alias}.table_name" if table == "source_registry" else
                f'{alias}."{columns[0]}"')
            before = "NULL" if operation == "INSERT" else _row_json("OLD", columns)
            after = "NULL" if operation == "DELETE" else _row_json("NEW", columns)
            conn.execute(f'''CREATE TRIGGER guard_{table}_{operation} BEFORE {operation} ON {table}
             BEGIN SELECT CASE WHEN cookies_writer_version() != {SCHEMA_VERSION}
             THEN RAISE(ABORT,'unsupported Cookies writer') END; END''')
            dirty = ""
            if table in ("cookies", "sources", "cookie_sources"):
                kind = "source" if table == "sources" else "cookie"
                key = f"{alias}.cookie_id" if table == "cookie_sources" else f"{alias}.id"
                dirty = f"INSERT OR IGNORE INTO foundation_dirty VALUES ('{kind}',{key});"
            review = ""
            if table == "pending_cookies":
                review = f"INSERT INTO review_events(cookie_id,operation,before_json,after_json,captured_at,transaction_id) VALUES ({alias}.id,'{operation}',{before},{after},cookies_now(),cookies_transaction_id());"
            conn.execute(f'''CREATE TRIGGER record_{table}_{operation} AFTER {operation} ON {table}
             BEGIN INSERT INTO write_events(transaction_id,table_name,operation,row_key,before_json,after_json,captured_at)
             VALUES(cookies_transaction_id(),'{table}','{operation}',{rowkey},{before},{after},cookies_now()); {dirty} {review} END''')
    for table in ("sources", "cookies", "cookie_sources"):
        conn.execute(f'''CREATE TRIGGER retain_{table} BEFORE DELETE ON {table}
         BEGIN SELECT RAISE(ABORT,'source unions and published identity cannot be deleted by the compatibility writer'); END''')
    for table in ("foundation_state", "documents", "document_identifiers", "input_observations",
                  "source_observation_heads", "stories", "insights", "insight_versions", "insight_evidence",
                  "presentation_revisions", "legacy_identities", "relationship_assessments", "processing_receipts",
                  "review_events", "write_events", "foundation_dirty"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            conn.execute(f'''CREATE TRIGGER writer_{table}_{operation} BEFORE {operation} ON {table}
             BEGIN SELECT CASE WHEN cookies_writer_version() != {SCHEMA_VERSION}
             THEN RAISE(ABORT,'unsupported Cookies writer') END; END''')
    immutable = ("documents", "document_identifiers", "input_observations", "insight_versions",
                 "insight_evidence", "presentation_revisions", "legacy_identities",
                 "relationship_assessments", "processing_receipts", "review_events", "write_events")
    for table in immutable:
        for operation in ("UPDATE", "DELETE"):
            conn.execute(f'''CREATE TRIGGER immutable_{table}_{operation} BEFORE {operation} ON {table}
             BEGIN SELECT RAISE(ABORT,'append-only structural history'); END''')


def _observe(conn, source_id, capture_kind):
    row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    if row is None:
        return None
    payload = dict(row)
    doc_id = _id("document", source_id)
    conn.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,?)",
                 (doc_id, source_id, row["source_id"], row["zeeker_url"], row["source_url"], row["title"], row["date"], _now()))
    conn.execute("INSERT OR IGNORE INTO document_identifiers VALUES (?,?,?,'recorded')",
                 (doc_id, "legacy_source_id", source_id))
    conn.execute("INSERT OR IGNORE INTO document_identifiers VALUES (?,?,?,'recorded')",
                 (doc_id, "observed_url", row["source_url"]))
    provenance = conn._provenance.get(source_id, {})
    serialized = _json({"source": payload, "provenance": provenance})
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    obs_id = _id("observation", source_id + "/" + digest)
    conn.execute("""INSERT OR IGNORE INTO input_observations VALUES
     (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
     (obs_id, doc_id, source_id, serialized, digest, row["raw_text"],
      hashlib.sha256(row["raw_text"].encode()).hexdigest(), provenance.get("input_kind"),
      provenance.get("selected_field"), provenance.get("input_projection"), provenance.get("upstream_version"),
      provenance.get("date_basis"), provenance.get("upstream_discovered_at"), provenance.get("upstream_enriched_at"),
      row["ingested_at"], _now(), capture_kind))
    conn.execute("INSERT INTO source_observation_heads VALUES (?,?) ON CONFLICT(source_id) DO UPDATE SET observation_id=excluded.observation_id", (source_id, obs_id))
    return obs_id


def _snapshot_cookie(conn, cookie_id, capture_kind):
    row = conn.execute("SELECT * FROM cookies WHERE id=?", (cookie_id,)).fetchone()
    if row is None:
        return
    now = _now()
    story_id = _id("story", cookie_id)
    conn.execute("INSERT OR IGNORE INTO stories VALUES (?,1,NULL,?,NULL,NULL,NULL,?)",
                 (story_id, row["admitted_at"], ("legacy_admitted" if row["admitted_at"] else "legacy_captured") if capture_kind == "migration_capture" else "admitted"))
    conn.execute("INSERT OR IGNORE INTO insights VALUES (?,?,NULL)", (cookie_id, story_id))
    source_ids = [r[0] for r in conn.execute("SELECT source_id FROM cookie_sources WHERE cookie_id=? ORDER BY source_id", (cookie_id,))]
    previous = conn.execute("SELECT * FROM presentation_revisions WHERE story_id=? ORDER BY revision DESC LIMIT 1", (story_id,)).fetchone()
    payload = _json(dict(row))
    if previous and previous["payload_json"] == payload and json.loads(previous["source_ids"]) == source_ids:
        return
    version = 1 if previous is None else previous["revision"] + 1
    revision_id, insight_version_id = str(uuid4()), str(uuid4())
    conn.execute("INSERT INTO insight_versions VALUES (?,?,?,?,?,?,?,?)",
                 (insight_version_id, cookie_id, version, payload, now, capture_kind, row["created_at"], conn.transaction_id()))
    for source_id in source_ids:
        head = conn.execute("SELECT observation_id FROM source_observation_heads WHERE source_id=?", (source_id,)).fetchone()
        if head:
            conn.execute("INSERT INTO insight_evidence VALUES (?,?,?,NULL)",
                         (insight_version_id, head[0], "legacy_association" if capture_kind == "migration_capture" else "associated"))
    conn.execute("INSERT INTO presentation_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (revision_id, story_id, version, _json([insight_version_id]), payload, _json(source_ids), now,
                  capture_kind, ("legacy_admission_recorded" if row["admitted_at"] else "legacy_state_captured") if capture_kind == "migration_capture" else "compatibility_writer",
                  None, None, conn.transaction_id()))
    conn.execute("UPDATE stories SET current_revision_id=? WHERE id=?", (revision_id, story_id))
    conn.execute("UPDATE insights SET current_version_id=? WHERE id=?", (insight_version_id, cookie_id))
    created = datetime.fromisoformat(row["created_at"])
    admitted = datetime.fromisoformat(row["admitted_at"]) if row["admitted_at"] else created
    # Preserve each channel's existing day semantics rather than normalising history.
    conn.execute("INSERT OR IGNORE INTO legacy_identities VALUES (?,?,?,?,?,?,?,?)",
                 (cookie_id, story_id, cookie_id, row["created_at"], row["admitted_at"], admitted.date().isoformat(),
                  admitted.astimezone(timezone.utc).date().isoformat() if row["admitted_at"] else None,
                  hashlib.sha256(cookie_id.encode()).hexdigest()[:12]))


def flush(conn):
    dirty = list(conn.execute("SELECT kind,id FROM foundation_dirty ORDER BY kind DESC,id"))
    # Sources first; an insight revision always points to a complete observation.
    for kind, key in dirty:
        if kind == "source":
            _observe(conn, key, "writer_capture")
    for kind, key in dirty:
        if kind == "cookie":
            _snapshot_cookie(conn, key, "writer_capture")
    conn.execute("DELETE FROM foundation_dirty")


def migrate(conn):
    """Idempotent, atomic structural migration; never group, rewrite or approve."""
    version = schema_version(conn)
    if version == SCHEMA_VERSION:
        return False
    if version:
        raise SchemaError(f"unsupported future schema {version}")
    if conn.in_transaction:
        raise SchemaError("migration requires a clean transaction boundary")
    conn._migrating = True
    conn.execute("BEGIN IMMEDIATE")
    try:
        _exec_ddl(conn, _DDL)
        conn.execute("INSERT INTO foundation_state VALUES (1,?,?,?)", (SCHEMA_VERSION, str(uuid4()), _now()))
        for row in conn.execute("SELECT id FROM sources ORDER BY id").fetchall():
            _observe(conn, row[0], "migration_capture")
        for row in conn.execute("SELECT id FROM cookies ORDER BY id").fetchall():
            _snapshot_cookie(conn, row[0], "migration_capture")
        for row in conn.execute("SELECT * FROM pending_cookies ORDER BY id").fetchall():
            conn.execute("INSERT INTO review_events(cookie_id,operation,before_json,after_json,captured_at,transaction_id) VALUES (?,'migration_capture',NULL,?,?,?)",
                         (row["id"], _json(dict(row)), _now(), conn.transaction_id()))
        _install_triggers(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn._migrating = False
    return True


def revision(conn, cookie_id):
    if not schema_version(conn):
        return 0
    row = conn.execute("SELECT max(version) FROM insight_versions WHERE insight_id=?", (cookie_id,)).fetchone()
    return row[0] or 0


def check_revision(conn, cookie_id, expected):
    current = revision(conn, cookie_id)
    if current and expected != current:
        raise StaleRevisionError("stale cookie revision; reload before saving")


def record_processing(conn, source_id, pipeline, extractor_version, output_ids):
    # Called within the same transaction as source, all outputs and metadata.
    flush(conn)
    head = conn.execute("SELECT observation_id FROM source_observation_heads WHERE source_id=?", (source_id,)).fetchone()
    conn.execute("INSERT OR IGNORE INTO processing_receipts VALUES (?,?,?,?,?,?,?)",
                 (str(uuid4()), source_id, head[0], pipeline, extractor_version, _json(output_ids), _now()))


def processing_complete(conn, source_id):
    return bool(conn.execute("SELECT 1 FROM processing_receipts WHERE source_id=? LIMIT 1", (source_id,)).fetchone())
