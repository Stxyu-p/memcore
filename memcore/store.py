"""
MemCore — storage layer.

open_store(): apply schema, run pending migrations under a lock, set pragmas.
All operations use short transactions and WAL + busy_timeout.
"""
import hashlib
import os
import pathlib
import re
import sqlite3
import struct
import time
import unicodedata

SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / 'schema' / 'schema.sql'
_MIGRATION_LOCK_VERSION = '__migration_lock__'
_MIGRATION_LOCK_TTL_SECONDS = 30.0
_MIGRATION_LOCK_WAIT_SECONDS = 10.0

# Migrations run in order; each is a list of SQL statements.
# 0001 = initial contract (schema.sql). Later migrations append here.
# schema.sql itself stays FROZEN — contract defects are reported, not patched.

_FTS_TRIGGERS = """
-- External-content FTS5 sync triggers (per schema.sql comment, Phase 1 duty).
CREATE TRIGGER IF NOT EXISTS memory_version_ai AFTER INSERT ON memory_version BEGIN
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_ad AFTER DELETE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_au AFTER UPDATE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
"""

_IDEMPOTENCY = """
-- Idempotency keys: unique constraint + INSERT OR IGNORE pattern (Phase 1).
CREATE TABLE IF NOT EXISTS idempotency_key (
    key         TEXT PRIMARY KEY,
    project_id  TEXT,
    memory_id   TEXT NOT NULL,
    version_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# 0004 — schema revision (ALTIMA Phase-1 verdict + MINORS V4 + minors 2/4).
# SQLite cannot ADD CONSTRAINT via ALTER: rebuild the three tables in one
# transaction, then rebuild FTS (rowids change) and enforce the rest with
# triggers where a rebuild is disproportionate.
_SCHEMA_REVISION = """
CREATE TABLE memory_new (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES project(id),
    scope               TEXT NOT NULL CHECK (scope IN ('project', 'private')),
    owner_agent_id      TEXT NOT NULL REFERENCES agent(id),
    type                TEXT NOT NULL DEFAULT 'fact',
    lifecycle           TEXT NOT NULL DEFAULT 'candidate'
                            CHECK (lifecycle IN ('candidate', 'accepted', 'conflict', 'superseded', 'rejected', 'disabled')),
    verification        TEXT NOT NULL DEFAULT 'unverified'
                            CHECK (verification IN ('unverified', 'source_backed', 'runtime_verified', 'user_authoritative')),
    freshness           TEXT NOT NULL DEFAULT 'current'
                            CHECK (freshness IN ('current', 'aging', 'stale')),
    current_version_id  TEXT REFERENCES memory_version(id)
                            DEFERRABLE INITIALLY DEFERRED,
    pinned              INTEGER NOT NULL DEFAULT 0,
    critical            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE memory_version_new (
    id                    TEXT PRIMARY KEY,
    memory_id             TEXT NOT NULL REFERENCES memory(id),
    content               TEXT NOT NULL,
    reason                TEXT,
    created_by_agent_id   TEXT NOT NULL REFERENCES agent(id),
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    validated_at          TEXT,
    valid_from            TEXT NOT NULL DEFAULT (datetime('now')),
    valid_until           TEXT,
    supersedes_version_id TEXT REFERENCES memory_version_new(id),
    source_hash           TEXT,
    source_commit         TEXT,
    source_file           TEXT
);
CREATE TABLE audit_event_new (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    action         TEXT NOT NULL,
    actor_agent_id TEXT REFERENCES agent(id),
    memory_id      TEXT REFERENCES memory(id),
    project_id     TEXT REFERENCES project(id),
    detail         TEXT DEFAULT '{}',
    write_key      TEXT UNIQUE,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO memory_new SELECT * FROM memory;
INSERT INTO memory_version_new SELECT * FROM memory_version;
INSERT INTO audit_event_new (id, action, actor_agent_id, memory_id, project_id, detail, created_at)
    SELECT id, action, actor_agent_id, memory_id, project_id, detail, created_at FROM audit_event;
DROP TABLE audit_event;
DROP TABLE memory_version;
DROP TABLE memory;
ALTER TABLE memory_new RENAME TO memory;
ALTER TABLE memory_version_new RENAME TO memory_version;
ALTER TABLE audit_event_new RENAME TO audit_event;
CREATE INDEX IF NOT EXISTS idx_memory_project ON memory(project_id);
CREATE INDEX IF NOT EXISTS idx_memory_owner ON memory(owner_agent_id);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(project_id, scope);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle ON memory(lifecycle);
CREATE INDEX IF NOT EXISTS idx_memory_version_memory ON memory_version(memory_id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_event(project_id);
CREATE INDEX IF NOT EXISTS idx_audit_memory ON audit_event(memory_id);
CREATE INDEX IF NOT EXISTS idx_tombstone_fingerprint ON tombstone(claim_fingerprint, scope);
CREATE INDEX IF NOT EXISTS idx_project_membership_agent ON project_membership(agent_id);
CREATE INDEX IF NOT EXISTS idx_project_membership_project ON project_membership(project_id);
DROP TRIGGER IF EXISTS memory_version_ai;
DROP TRIGGER IF EXISTS memory_version_ad;
DROP TRIGGER IF EXISTS memory_version_au;
INSERT INTO memory_version_fts(memory_version_fts) VALUES ('rebuild');
CREATE TRIGGER IF NOT EXISTS memory_version_ai AFTER INSERT ON memory_version BEGIN
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_ad AFTER DELETE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
END;
CREATE TRIGGER IF NOT EXISTS memory_version_au AFTER UPDATE ON memory_version BEGIN
  INSERT INTO memory_version_fts(memory_version_fts, rowid, content)
  VALUES ('delete', old.rowid, old.content);
  INSERT INTO memory_version_fts(rowid, content) VALUES (new.rowid, new.content);
END;
CREATE TRIGGER IF NOT EXISTS audit_idempotency_enforce BEFORE INSERT ON audit_event
WHEN new.write_key IS NOT NULL AND EXISTS (
  SELECT 1 FROM audit_event WHERE write_key = new.write_key
) BEGIN
  SELECT RAISE(IGNORE);
END;
CREATE TRIGGER IF NOT EXISTS audit_created_at_iso BEFORE INSERT ON audit_event
WHEN new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
 AND new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
BEGIN
  SELECT RAISE(IGNORE);
END;
"""

# Post-revision DDL that runs AFTER the rebuild (must exist for the FTS
# rebuild to be consistent, and for normalized timestamps going forward).
_ISO_NORMALIZE = """
-- Guard: reject malformed timestamps. Engine writes ISO 8601 Z explicitly;
-- legacy datetime('now') values (defaults, manual SQL) remain accepted.
CREATE TRIGGER IF NOT EXISTS memory_version_created_at_iso BEFORE INSERT ON memory_version
WHEN new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
 AND new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
BEGIN
  SELECT RAISE(IGNORE);
END;
"""

_INGEST_JOURNAL = """
CREATE TABLE IF NOT EXISTS ingest_event (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL REFERENCES project(id),
    agent_id          TEXT NOT NULL REFERENCES agent(id),
    session_id        TEXT NOT NULL DEFAULT '',
    event_type        TEXT NOT NULL CHECK (event_type IN ('turn','memory_write','delegation','session_end','manual')),
    user_content      TEXT NOT NULL DEFAULT '',
    assistant_content TEXT NOT NULL DEFAULT '',
    metadata          TEXT NOT NULL DEFAULT '{}',
    content_hash      TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processed','ignored','failed')),
    decision          TEXT,
    error             TEXT,
    created_at        TEXT NOT NULL,
    processed_at      TEXT,
    UNIQUE(project_id, agent_id, session_id, content_hash)
);
CREATE INDEX IF NOT EXISTS idx_ingest_event_status ON ingest_event(project_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_ingest_event_agent ON ingest_event(agent_id, created_at);
CREATE TABLE IF NOT EXISTS ingest_derivation (
    event_id   TEXT NOT NULL REFERENCES ingest_event(id) ON DELETE CASCADE,
    memory_id  TEXT NOT NULL REFERENCES memory(id),
    relation   TEXT NOT NULL CHECK (relation IN ('created','duplicate','evidence','corrected','ignored')),
    created_at TEXT NOT NULL,
    PRIMARY KEY(event_id, memory_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_ingest_derivation_memory ON ingest_derivation(memory_id);
"""

_SEMANTIC_ANALYSIS = """
CREATE TABLE IF NOT EXISTS ingest_analysis (
    id                TEXT PRIMARY KEY,
    event_id          TEXT NOT NULL REFERENCES ingest_event(id) ON DELETE CASCADE,
    analyzer          TEXT NOT NULL,
    verdict           TEXT NOT NULL CHECK (verdict IN ('remember','ignore','defer')),
    candidate_content TEXT NOT NULL DEFAULT '',
    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    rationale         TEXT NOT NULL DEFAULT '',
    metadata          TEXT NOT NULL DEFAULT '{}',
    memory_id         TEXT REFERENCES memory(id),
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_analysis_event ON ingest_analysis(event_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ingest_analysis_analyzer ON ingest_analysis(analyzer, created_at);
"""

_INTEGRITY_HARDENING = """
DROP TRIGGER IF EXISTS memory_version_created_at_iso;
CREATE TRIGGER memory_version_created_at_iso BEFORE INSERT ON memory_version
WHEN new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9]'
 AND new.created_at NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
BEGIN
  SELECT RAISE(ABORT, 'invalid memory_version.created_at');
END;
CREATE UNIQUE INDEX IF NOT EXISTS idx_project_name_unique ON project(name);
"""

_PERFORMANCE_FAST_PATHS = """
ALTER TABLE memory ADD COLUMN claim_fingerprint TEXT;
CREATE INDEX IF NOT EXISTS idx_memory_private_claim
ON memory(project_id, owner_agent_id, scope, claim_fingerprint, lifecycle);
CREATE INDEX IF NOT EXISTS idx_ingest_event_pending_decision
ON ingest_event(project_id, agent_id, decision, created_at, id)
WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_ingest_derivation_memory_event
ON ingest_derivation(memory_id, event_id);
CREATE INDEX IF NOT EXISTS idx_audit_mutation_recovery
ON audit_event(project_id, actor_agent_id, action, id DESC);
"""

_PERFORMANCE_ROUND2 = """
CREATE INDEX IF NOT EXISTS idx_memory_project_claim
ON memory(project_id, scope, claim_fingerprint, lifecycle);
"""

# Data-only repair. Python recomputes Unicode-normalized fingerprints and
# migrates fingerprint-derived tombstone/idempotency references transactionally.
_UNICODE_FINGERPRINT_REPAIR = "SELECT 1;"

_CURRENT_VERSION_OWNERSHIP = """
CREATE TRIGGER IF NOT EXISTS memory_current_version_owner_insert
BEFORE INSERT ON memory
WHEN new.current_version_id IS NOT NULL AND EXISTS (
  SELECT 1 FROM memory_version v
  WHERE v.id=new.current_version_id AND v.memory_id != new.id
) BEGIN
  SELECT RAISE(ABORT, 'current_version_id belongs to another memory');
END;
CREATE TRIGGER IF NOT EXISTS memory_current_version_owner_update
BEFORE UPDATE OF current_version_id ON memory
WHEN new.current_version_id IS NOT NULL AND EXISTS (
  SELECT 1 FROM memory_version v
  WHERE v.id=new.current_version_id AND v.memory_id != new.id
) BEGIN
  SELECT RAISE(ABORT, 'current_version_id belongs to another memory');
END;
CREATE TRIGGER IF NOT EXISTS memory_version_current_owner_insert
BEFORE INSERT ON memory_version
WHEN EXISTS (
  SELECT 1 FROM memory m
  WHERE m.current_version_id=new.id AND m.id != new.memory_id
) BEGIN
  SELECT RAISE(ABORT, 'memory_version does not own referencing memory');
END;
"""

_PROVENANCE_SEAL = """
-- 0014: tamper-evident seal on journal writes (Phase 6a).
-- Every ingest_event row carries an HMAC-SHA256 over
-- (project_id, agent_id, content_hash) keyed by the store-local provenance
-- key, so post-write tampering with attribution fields is detectable.
-- Pre-migration rows get NULL seals and are reported as 'unsealed', never
-- as failures.
--
-- NOTE: the ALTER TABLE lives in Python (_ensure_provenance_column), not
-- here, because ADD COLUMN is not idempotent: wiping schema_migrations in
-- tests (or a crashed first run) would replay it against an existing
-- column and fail with "duplicate column name".
CREATE TABLE IF NOT EXISTS provenance_key (
    key_id       TEXT PRIMARY KEY,
    key_material TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    rotated_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ingest_event_seal ON ingest_event(provenance_seal);
"""


_REINFORCEMENT_DECAY = """
-- 0015: reinforcement-aware decay (Phase 6b).
-- recall_count / last_recalled record that a memory proved useful in
-- retrieval. apply_freshness_decay skips recently-recalled rows, so used
-- facts reinforce and never-recalled facts fade. Pure clock decay stays as
-- the fallback for rows never recalled.
-- NOTE: ALTER TABLE lives in Python like 0014 (ADD COLUMN is not idempotent).
CREATE INDEX IF NOT EXISTS idx_memory_last_recalled ON memory(last_recalled);
"""


_SCOPE_DETAIL = """
-- 0016: optional scope subdivision tag (Phase 6d).
-- scope itself stays project/private (governance untouched). scope_detail
-- holds 'skill:<name>' / 'episode:<id>' / 'session:<id>' or NULL for
-- untagged rows. Read rule unchanged: project -> members, anything else ->
-- owner only. Recall can filter by detail opt-in.
-- NOTE: ALTER TABLE lives in Python like 0014/0015 (not idempotent in SQL).
CREATE INDEX IF NOT EXISTS idx_memory_scope_detail
    ON memory(project_id, scope_detail);
"""


_BITEMPORAL_VALID_UNTIL = """
-- 0017: close the bi-temporal loop (Phase 6e).
-- memory_version.valid_from has existed since the frozen schema but nothing
-- ever closed a version's validity window. This adds valid_until, backfilled
-- so every superseded version ends when its successor begins; current tips
-- stay open (NULL). Point-in-time recall filters on the window.
-- NOTE: ALTER TABLE lives in Python like 0014-0016 (not idempotent in SQL).
CREATE INDEX IF NOT EXISTS idx_memory_version_validity
    ON memory_version(memory_id, valid_from, valid_until);
"""

_MEMORY_EMBEDDING = """
-- 0018: local vector embedding storage for semantic hybrid recall.
CREATE TABLE IF NOT EXISTS memory_embedding (
    memory_id   TEXT NOT NULL,
    version_id  TEXT NOT NULL,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (memory_id, version_id),
    FOREIGN KEY (memory_id) REFERENCES memory(id) ON DELETE CASCADE,
    FOREIGN KEY (version_id) REFERENCES memory_version(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_embedding_version ON memory_embedding(version_id);
"""


MIGRATIONS = [
    ('0001_initial_contract', None),  # None = apply schema.sql verbatim
    ('0002_fts_sync_triggers', _FTS_TRIGGERS),
    ('0003_idempotency_keys', _IDEMPOTENCY),
    ('0004_schema_revision', _SCHEMA_REVISION),
    ('0005_iso_timestamps', _ISO_NORMALIZE),
    ('0006_tombstone_override_fk', """
CREATE TABLE tombstone_new (
    id                TEXT PRIMARY KEY,
    claim_fingerprint TEXT NOT NULL,
    scope             TEXT NOT NULL,
    reason            TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    overridden_by     TEXT REFERENCES agent(id)
);
INSERT INTO tombstone_new SELECT * FROM tombstone;
DROP TABLE tombstone;
ALTER TABLE tombstone_new RENAME TO tombstone;
CREATE INDEX IF NOT EXISTS idx_tombstone_fingerprint ON tombstone(claim_fingerprint, scope);
"""),
    ('0007_integrity_hardening', _INTEGRITY_HARDENING),
    ('0008_ingest_journal', _INGEST_JOURNAL),
    ('0009_semantic_analysis', _SEMANTIC_ANALYSIS),
    ('0010_performance_fast_paths', _PERFORMANCE_FAST_PATHS),
    ('0011_performance_round2', _PERFORMANCE_ROUND2),
    ('0012_unicode_fingerprint_repair', _UNICODE_FINGERPRINT_REPAIR),
    ('0013_current_version_ownership', _CURRENT_VERSION_OWNERSHIP),
    ('0014_provenance_seal', _PROVENANCE_SEAL),
    ('0015_reinforcement_decay', _REINFORCEMENT_DECAY),
    ('0016_scope_detail', _SCOPE_DETAIL),
    ('0017_bitemporal_valid_until', _BITEMPORAL_VALID_UNTIL),
    ('0018_memory_embedding', _MEMORY_EMBEDDING),
]


class StoreError(Exception):
    pass


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _migration_history_violations(conn):
    """Return gaps/unknown entries in the applied migration prefix."""
    if not _table_exists(conn, 'schema_migrations'):
        return []
    applied = {
        row[0] for row in conn.execute(
            'SELECT version FROM schema_migrations WHERE version != ?',
            (_MIGRATION_LOCK_VERSION,)
        ).fetchall()
    }
    if not applied:
        return []
    known = [name for name, _sql in MIGRATIONS]
    unknown = sorted(applied.difference(known))
    violations = [('unknown', value) for value in unknown]
    known_applied = [name for name in known if name in applied]
    if known_applied:
        highest = known.index(known_applied[-1])
        for missing in known[:highest + 1]:
            if missing not in applied:
                violations.append(('missing', missing))
    return violations


def _current_version(conn):
    if not _table_exists(conn, 'schema_migrations'):
        return None
    cur = conn.execute(
        'SELECT version FROM schema_migrations WHERE version != ? '
        'ORDER BY applied_at DESC, rowid DESC LIMIT 1',
        (_MIGRATION_LOCK_VERSION,)
    )
    row = cur.fetchone()
    return row[0] if row else None


#: Maximum fraction of fully-zero 4K pages tolerated when opening an
#: existing store. The 2026-10-03 incident left 683/710 pages zeroed (96.6%)
#: with a valid header — SQLite opened it without complaint and every query
#: then failed with "database disk image is malformed". A healthy store of
#: this size has no fully-zero pages outside the freelist; anything above 50%
#: is not SQLite behaviour but an external writer (snapshot/restore tooling,
#: AV quarantine, truncated copy) having damaged the file.
ZERO_PAGE_RATIO_LIMIT = 0.5


def _zero_page_ratio(path: pathlib.Path, sample_pages: int = 64) -> float:
    """Fraction of sampled 4K pages that are entirely zero.

    Reads the first ``sample_pages`` pages only, so the check costs one
    256KB read regardless of store size. Returns 0.0 for missing/empty files
    (creation path) and 1.0 when nothing could be read.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0.0
    if size == 0:
        return 0.0
    pages = min(sample_pages, size // 4096)
    if pages == 0:
        return 0.0
    zeroed = 0
    with open(path, 'rb') as fh:
        for _ in range(pages):
            chunk = fh.read(4096)
            if len(chunk) < 4096:
                break
            if not any(chunk):
                zeroed += 1
    return zeroed / pages


def open_store(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """
    Open (creating if needed) a MemCore store.

    Returns a sqlite3.Connection with pragmas set and all migrations applied,
    under a migration lock so two processes booting concurrently are safe.

    An existing file whose sampled pages are mostly zero is refused outright:
    that is the signature of the 2026-10-03 external-writer incident, and
    opening it would only surface as confusing per-query malformed errors.
    """
    p = pathlib.Path(db_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.is_file():
        ratio = _zero_page_ratio(p)
        if ratio > ZERO_PAGE_RATIO_LIMIT:
            raise StoreError(
                f'store at {p} looks externally damaged '
                f'({ratio:.0%} of sampled pages are zero; '
                f'incident pattern 2026-10-03). Refusing to open. '
                f'Restore with: memcore restore-from-snapshot --snapshot <file> --confirm'
            )
    conn = sqlite3.connect(
        str(p), timeout=10, isolation_level=None,
        check_same_thread=check_same_thread
    )
    try:
        # Set the busy timeout before WAL negotiation: concurrent first boots
        # can otherwise race on PRAGMA journal_mode before SQLite has a chance
        # to wait for the other connection's schema lock.
        conn.execute('PRAGMA busy_timeout = 10000')
        deadline = time.time() + 10.0
        while True:
            try:
                conn.execute('PRAGMA journal_mode = WAL')
                break
            except sqlite3.OperationalError as e:
                if 'locked' not in str(e).lower() and 'busy' not in str(e).lower():
                    raise
                if time.time() >= deadline:
                    raise
                time.sleep(0.05)
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('PRAGMA synchronous = NORMAL')
        apply_migrations(conn)
        return conn
    except Exception:
        conn.close()
        raise


def _open_existing_connection(db_path: str, *, readonly: bool,
                              check_same_thread: bool = True) -> sqlite3.Connection:
    """Open an existing store without WAL negotiation or migration work.

    This is the hot-path opener for a long-running provider after ``open_store``
    has already completed bootstrap/migrations during initialization. ``mode=rw``
    and ``mode=ro`` both fail if the database disappears, so runtime calls never
    create an accidental empty store.
    """
    p = pathlib.Path(db_path).expanduser().resolve()
    if not p.is_file():
        raise StoreError(f'store does not exist: {p}')
    mode = 'ro' if readonly else 'rw'
    conn = sqlite3.connect(
        p.as_uri() + f'?mode={mode}', uri=True, timeout=10,
        isolation_level=None, check_same_thread=check_same_thread
    )
    try:
        conn.execute('PRAGMA busy_timeout = 5000')
        conn.execute('PRAGMA foreign_keys = ON')
        if readonly:
            conn.execute('PRAGMA query_only = ON')
        else:
            conn.execute('PRAGMA synchronous = NORMAL')
        return conn
    except Exception:
        conn.close()
        raise


def open_runtime_store(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Fast writable opener; requires a store already bootstrapped by ``open_store``."""
    return _open_existing_connection(
        db_path, readonly=False, check_same_thread=check_same_thread
    )


def open_runtime_store_readonly(db_path: str) -> sqlite3.Connection:
    """Fast read-only opener for a store validated during provider initialization."""
    return _open_existing_connection(db_path, readonly=True)


#: Backup freshness contract. A store with no recent verified backup has no
#: recovery path, which is exactly the condition that let a zero-filled
#: memory.db go unnoticed for months on 2026-10-03.
BACKUP_DIRNAME = 'backups'
BACKUP_MAX_AGE_DAYS = 7
BACKUP_MIN_COUNT = 3

#: Snapshot filenames this module owns: ``<store-stem>-<YYYYmmddTHHMMSSZ>.db``.
#: Anything else in the backup directory is a hand-made or legacy file and must
#: not be counted as a managed recovery point.
_SNAPSHOT_RE = re.compile(
    r'^(?P<stem>.+)-(?P<stamp>\d{8}T\d{6}Z)\.db$'
)


def _is_managed_snapshot(path: pathlib.Path, stem: str) -> bool:
    match = _SNAPSHOT_RE.match(path.name)
    return bool(match) and match.group('stem') == stem


def _managed_snapshots(backup_dir: pathlib.Path, stem: str):
    """Snapshots written by ``backup_store``, newest first, by modification time."""
    if not backup_dir.is_dir():
        return []
    candidates = [
        p for p in backup_dir.glob('*.db')
        if p.is_file() and _is_managed_snapshot(p, stem)
    ]
    return sorted(
        candidates,
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )


def backup_store(source_db: str, *, keep: int = 14) -> pathlib.Path:
    """Snapshot a store with SQLite's own online backup API.

    Uses ``Connection.backup`` rather than a file copy: it is transaction-safe
    against concurrent writers and produces a consistent image even while WAL
    traffic is in flight. A plain ``shutil.copy`` of a WAL-mode database can
    capture a torn state.

    The snapshot is written to a temporary file, integrity-checked, then moved
    into place, so a failure never leaves a corrupt file in the backup dir.
    """
    src_path = pathlib.Path(source_db).expanduser()
    if not src_path.is_file():
        raise StoreError(f'store does not exist: {src_path}')
    backup_dir = src_path.parent / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    dest = backup_dir / f'{src_path.stem}-{stamp}.db'
    tmp = dest.with_suffix('.db.partial')

    src = sqlite3.connect(str(src_path), timeout=10, isolation_level=None)
    try:
        dst = sqlite3.connect(str(tmp), timeout=10, isolation_level=None)
        try:
            src.backup(dst)
            row = dst.execute('PRAGMA integrity_check').fetchone()
            if not row or row[0] != 'ok':
                raise StoreError(
                    f'backup snapshot failed integrity check: {row[0] if row else "no result"}'
                )
        finally:
            dst.close()
        os.replace(tmp, dest)
    except Exception:
        for leftover in (tmp,):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    finally:
        src.close()

    _prune_backups(backup_dir, keep, src_path.stem)
    return dest



def _prune_backups(backup_dir: pathlib.Path, keep: int, stem: str) -> None:
    """Retain the ``keep`` newest managed snapshots; never touch other files."""
    if keep < 1:
        return
    for stale in _managed_snapshots(backup_dir, stem)[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def verify_backups(source_db: str, *, max_age_days: int = BACKUP_MAX_AGE_DAYS,
                   min_count: int = BACKUP_MIN_COUNT) -> dict:
    """Content-free recovery-readiness report for a store's backup set.

    ``recovery_ready`` means exactly one thing: a verified snapshot of this store
    exists and is younger than ``max_age_days``. Snapshot count is reported but
    deliberately does NOT gate readiness — every snapshot passes an integrity
    check at creation time, so one good snapshot is a usable recovery path.
    Requiring several would make a freshly provisioned store permanently
    "unhealthy" for no real gain.

    Only files this module wrote are counted, so a hand-placed .bak in the
    directory cannot make a store look recoverable. Never reads memory text.
    """
    src_path = pathlib.Path(source_db).expanduser()
    backup_dir = src_path.parent / BACKUP_DIRNAME
    report = {
        'backup_dir': str(backup_dir),
        'backup_dir_exists': backup_dir.is_dir(),
        'snapshot_count': 0,
        'newest_snapshot': None,
        'newest_age_days': None,
        'oldest_age_days': None,
        'max_age_days': max_age_days,
        'min_count': min_count,
        'recovery_ready': False,
        'problems': [],
    }
    if not report['backup_dir_exists']:
        report['problems'].append('no_backup_dir')
        return report

    snapshots = _managed_snapshots(backup_dir, src_path.stem)
    report['snapshot_count'] = len(snapshots)
    if not snapshots:
        report['problems'].append('no_snapshots')
        return report

    now = time.time()
    ages = []
    for path in snapshots:
        try:
            ages.append((now - path.stat().st_mtime) / 86400.0)
        except OSError:
            continue
    if not ages:
        report['problems'].append('unreadable_snapshot_metadata')
        return report

    report['newest_age_days'] = round(min(ages), 2)
    report['oldest_age_days'] = round(max(ages), 2)
    report['newest_snapshot'] = snapshots[0].name

    if min(ages) > max_age_days:
        report['problems'].append(f'stale_backup:{report["newest_age_days"]}d')
        report['recovery_ready'] = False
    else:
        report['recovery_ready'] = True
        if len(snapshots) < min_count:
            # Informational only: a verified snapshot is a recovery path on its
            # own; the target count matters for depth, not for recoverability.
            report['problems'].append(f'below_target_snapshots:{len(snapshots)}')
    return report


def corroboration_funnel(conn, accept_n: int = 3, golden_n: int = 5) -> dict:
    """Content-free corroboration funnel for one store.

    Reports how live (candidate/accepted/conflict) claim fingerprints are
    distributed by distinct writer count: at_1, at_2 .., at_accept,
    at_golden. Never reads memory text — only fingerprint group sizes and
    owner-agent counts.

    This is the number that tells whether ADR-0018's Golden Rule can ever
    fire. On 2026-10-03 the live store held 73 fingerprints, ALL at sources=1,
    which is why `corroborate` had never promoted anything. The mechanism was
    correct; it had no reachable inputs.
    """
    rows = conn.execute(
        "SELECT fp, sources FROM ("
        "SELECT claim_fingerprint AS fp, COUNT(DISTINCT owner_agent_id) AS sources "
        "FROM memory WHERE claim_fingerprint IS NOT NULL "
        "AND lifecycle IN ('candidate','accepted','conflict') "
        "GROUP BY claim_fingerprint)"
    ).fetchall()
    at_1 = sum(1 for _, s in rows if s == 1)
    at_2 = sum(1 for _, s in rows if s == 2)
    at_accept = sum(1 for _, s in rows if s >= accept_n)
    at_golden = sum(1 for _, s in rows if s >= golden_n)
    return {
        'fingerprints': len(rows),
        'at_1': at_1,
        'at_2': at_2,
        'at_accept': at_accept,
        'at_golden': at_golden,
        'accept_n': accept_n,
        'golden_n': golden_n,
        'reachable': at_accept > 0,
    }


def _active_provenance_key(conn) -> tuple:
    """Return (key_id, key_material bytes), creating the key on first use.

    The key is store-local and random (os.urandom 32 bytes, hex-encoded). It
    is not a high-grade secret — it lives next to the data — but it makes
    post-write attribution tampering detectable, which is the threat that
    matters here (a writer claiming another agent's identity after the fact).
    """
    import hashlib as _hashlib
    import hmac as _hmac
    import os as _os
    row = conn.execute(
        'SELECT key_id, key_material FROM provenance_key '
        "ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    if row:
        return row[0], bytes.fromhex(row[1])
    key_id = 'pk-' + _os.urandom(8).hex()
    key_material = _os.urandom(32).hex()
    conn.execute(
        'INSERT INTO provenance_key (key_id, key_material) VALUES (?, ?)',
        (key_id, key_material),
    )
    return key_id, bytes.fromhex(key_material)


def seal_event(project_id: str, agent_id: str, content_hash: str,
               key_material: bytes) -> str:
    """HMAC-SHA256 seal over the attribution triple. Stdlib only."""
    import hashlib as _hashlib
    import hmac as _hmac
    msg = f'{project_id}\0{agent_id}\0{content_hash}'.encode('utf-8')
    return _hmac.new(key_material, msg, _hashlib.sha256).hexdigest()


def verify_event_seal(conn, event_id: str) -> dict:
    """Check one event's seal. Returns {sealed, valid, key_id}.

    Unsealed (pre-migration) rows report sealed=False, valid=None — they are
    historical, not failures.
    """
    row = conn.execute(
        'SELECT project_id, agent_id, content_hash, provenance_seal '
        'FROM ingest_event WHERE id=?', (event_id,),
    ).fetchone()
    if row is None:
        return {'sealed': False, 'valid': False, 'key_id': None,
                'reason': 'event_not_found'}
    project_id, agent_id, content_hash, seal = row
    if not seal:
        return {'sealed': False, 'valid': None, 'key_id': None,
                'reason': 'pre_migration_row'}
    key_row = conn.execute(
        'SELECT key_id, key_material FROM provenance_key '
        'ORDER BY created_at DESC LIMIT 1'
    ).fetchone()
    if key_row is None:
        return {'sealed': True, 'valid': False, 'key_id': None,
                'reason': 'no_key'}
    key_id, key_material = key_row[0], bytes.fromhex(key_row[1])
    expected = seal_event(project_id, agent_id, content_hash, key_material)
    import hmac as _hmac
    valid = _hmac.compare_digest(expected, seal)
    return {'sealed': True, 'valid': valid, 'key_id': key_id,
            'reason': 'ok' if valid else 'mismatch'}


def verify_all_seals(conn, limit: int = 10000) -> dict:
    """Content-free seal census: {sealed, valid, invalid, unsealed}.

    Never reads event text — only IDs and seal columns.
    """
    rows = conn.execute(
        'SELECT id FROM ingest_event ORDER BY created_at DESC LIMIT ?',
        (limit,),
    ).fetchall()
    report = {'checked': 0, 'sealed': 0, 'valid': 0, 'invalid': 0,
              'unsealed': 0, 'invalid_ids': []}
    for (event_id,) in rows:
        result = verify_event_seal(conn, event_id)
        report['checked'] += 1
        if not result['sealed']:
            report['unsealed'] += 1
        elif result['valid']:
            report['sealed'] += 1
            report['valid'] += 1
        else:
            report['sealed'] += 1
            report['invalid'] += 1
            if len(report['invalid_ids']) < 10:
                report['invalid_ids'].append(event_id)
    return report


def pack_vector(floats: list[float]) -> bytes:
    """Pack a list of float numbers into an IEEE 754 float32 little-endian binary BLOB."""
    return struct.pack(f'<{len(floats)}f', *floats)


def unpack_vector(blob: bytes) -> list[float]:
    """Unpack an IEEE 754 float32 little-endian binary BLOB into a list of floats."""
    count = len(blob) // 4
    return list(struct.unpack(f'<{count}f', blob))


def store_embedding(conn: sqlite3.Connection, memory_id: str, version_id: str,
                    model: str, vector: list[float]) -> None:
    """Persist an embedding vector for a specific memory version."""
    blob = pack_vector(vector)
    dim = len(vector)
    conn.execute(
        'INSERT OR REPLACE INTO memory_embedding '
        '(memory_id, version_id, model, dim, vector) '
        'VALUES (?, ?, ?, ?, ?)',
        (memory_id, version_id, model, dim, blob)
    )


def get_embedding(conn: sqlite3.Connection, memory_id: str,
                  version_id: str) -> list[float] | None:
    """Retrieve and unpack the embedding vector for a memory version, or None if absent."""
    cur = conn.execute(
        'SELECT vector FROM memory_embedding WHERE memory_id=? AND version_id=?',
        (memory_id, version_id)
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    return unpack_vector(row[0])


def open_store_readonly(db_path: str) -> sqlite3.Connection:
    """Open an existing, current MemCore store without schema/domain writes."""
    conn = _open_existing_connection(db_path, readonly=True)
    try:
        history_violations = _migration_history_violations(conn)
        if history_violations:
            raise StoreError(f'invalid migration history: {history_violations[:5]}')
        current = _current_version(conn)
        expected = MIGRATIONS[-1][0]
        if current != expected:
            raise StoreError(
                f'read-only store schema is {current or "unversioned"}; '
                f'expected {expected}. Open normally to migrate first.'
            )
        return conn
    except Exception:
        conn.close()
        raise


def apply_migrations(conn):
    """Bootstrap migration bookkeeping and apply all pending migrations.

    Shared by open_store() and the fixtures (which bring their own connection).
    """
    if not _table_exists(conn, 'schema_migrations'):
        conn.execute(
            'CREATE TABLE IF NOT EXISTS schema_migrations ('
            '  version TEXT PRIMARY KEY,'
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now')), "
            '  lock_holder TEXT,'
            '  lock_until REAL)'
        )
    _run_migrations(conn)


def _migration_lock_row(conn):
    return conn.execute(
        'SELECT lock_holder, lock_until FROM schema_migrations WHERE version=?',
        (_MIGRATION_LOCK_VERSION,)
    ).fetchone()


def _acquire_migration_lock(conn):
    """Acquire a crash-visible migration lock row, reclaiming only expired locks."""
    holder = f'pid:{os.getpid()}:conn:{id(conn)}'
    deadline = time.time() + _MIGRATION_LOCK_WAIT_SECONDS
    while True:
        now = time.time()
        try:
            conn.execute('BEGIN IMMEDIATE')
            row = _migration_lock_row(conn)
            if row and row[0] and row[1] is not None and row[1] > now and row[0] != holder:
                conn.execute('ROLLBACK')
                if time.time() >= deadline:
                    raise StoreError(
                        f'migration lock held by {row[0]} until {row[1]:.3f}'
                    )
                time.sleep(0.05)
                continue
            conn.execute(
                'INSERT INTO schema_migrations '
                '(version, applied_at, lock_holder, lock_until) '
                "VALUES (?, datetime('now'), ?, ?) "
                'ON CONFLICT(version) DO UPDATE SET '
                "applied_at=datetime('now'), lock_holder=excluded.lock_holder, "
                'lock_until=excluded.lock_until',
                (_MIGRATION_LOCK_VERSION, holder,
                 now + _MIGRATION_LOCK_TTL_SECONDS)
            )
            conn.execute('COMMIT')
            return holder
        except StoreError:
            raise
        except sqlite3.OperationalError as e:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            if time.time() >= deadline:
                raise StoreError(f'could not acquire migration lock: {e}') from e
            time.sleep(0.05)


def _release_migration_lock(conn, holder):
    deadline = time.time() + _MIGRATION_LOCK_WAIT_SECONDS
    while True:
        try:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                'DELETE FROM schema_migrations WHERE version=? AND lock_holder=?',
                (_MIGRATION_LOCK_VERSION, holder)
            )
            conn.execute('COMMIT')
            return
        except sqlite3.OperationalError as e:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            if time.time() >= deadline:
                raise StoreError(f'could not release migration lock: {e}') from e
            time.sleep(0.05)


def _renew_migration_lock(conn, holder):
    """Renew only while this process still owns the migration lease."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        cur = conn.execute(
            'UPDATE schema_migrations SET lock_until=? '
            'WHERE version=? AND lock_holder=?',
            (time.time() + _MIGRATION_LOCK_TTL_SECONDS,
             _MIGRATION_LOCK_VERSION, holder)
        )
        if cur.rowcount != 1:
            raise StoreError('migration lock ownership was lost')
        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise


def _run_migrations(conn):
    """Apply pending migrations under one crash-visible process lock."""
    known = [v for v, _ in MIGRATIONS]
    history_violations = _migration_history_violations(conn)
    if history_violations:
        raise StoreError(f'invalid migration history: {history_violations[:5]}')
    version = _current_version(conn)
    if version is not None and version not in known:
        raise StoreError(f'unsupported schema migration version: {version}')

    has_pending = version is None or version != known[-1]
    if not has_pending and _migration_lock_row(conn) is None:
        return

    holder = _acquire_migration_lock(conn)
    try:
        # Re-read after acquiring: another process may have completed while
        # this connection was waiting for the lock.
        history_violations = _migration_history_violations(conn)
        if history_violations:
            raise StoreError(f'invalid migration history: {history_violations[:5]}')
        version = _current_version(conn)
        if version is None:
            _apply_migration(
                conn, '0001_initial_contract',
                SCHEMA_PATH.read_text(encoding='utf-8')
            )
            pending = MIGRATIONS[1:]
        else:
            if version not in known:
                raise StoreError(f'unsupported schema migration version: {version}')
            pending = MIGRATIONS[known.index(version) + 1:]
        for name, sql in pending:
            # Renew transactionally and verify ownership before every step.
            # If another process reclaimed an expired lease after a long
            # migration, this process must stop instead of interleaving.
            _renew_migration_lock(conn, holder)
            _apply_migration(conn, name, sql)
    finally:
        _release_migration_lock(conn, holder)


def _script_statements(sql):
    """Yield complete SQLite statements, preserving trigger BEGIN/END blocks."""
    buf = ''
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt:
                yield stmt
            buf = ''
    if buf.strip():
        raise StoreError('incomplete SQL statement in migration')


def _legacy_fingerprint(content):
    """Fingerprint algorithm used by migration 0010 before Unicode NFC hardening."""
    normalized = ' '.join(str(content or '').lower().strip().split())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _canonical_fingerprint(content):
    """Storage-side copy of core.fingerprint() without importing core circularly."""
    normalized = unicodedata.normalize(
        'NFC', ' '.join(str(content or '').lower().strip().split())
    )
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _backfill_current_fingerprints(conn):
    """Populate the indexed fingerprint for memories created before migration 0010."""
    rows = conn.execute(
        'SELECT m.id, v.content FROM memory m '
        'JOIN memory_version v ON v.id=m.current_version_id AND v.memory_id=m.id '
        'WHERE m.claim_fingerprint IS NULL'
    ).fetchall()
    updates = [(_canonical_fingerprint(content), memory_id)
               for memory_id, content in rows]
    if updates:
        conn.executemany(
            'UPDATE memory SET claim_fingerprint=? WHERE id=?', updates
        )


def _current_version_ownership_violations(conn):
    """Rows whose current-version pointer is absent or owned by another memory."""
    return conn.execute(
        'SELECT m.id, m.current_version_id, v.memory_id FROM memory m '
        'LEFT JOIN memory_version v ON v.id=m.current_version_id '
        'WHERE m.current_version_id IS NULL OR v.id IS NULL OR v.memory_id != m.id '
        'ORDER BY m.id'
    ).fetchall()


def _repair_unicode_fingerprints(conn):
    """Repair pre-NFC fingerprints plus their durable refusal/idempotency references."""
    versions = conn.execute(
        'SELECT id, content FROM memory_version'
    ).fetchall()
    remap = {}
    canonical_by_version = {}
    for version_id, content in versions:
        old_fp = _legacy_fingerprint(content)
        new_fp = _canonical_fingerprint(content)
        canonical_by_version[version_id] = new_fp
        if old_fp != new_fp:
            remap.setdefault(old_fp, set()).add(new_fp)

    # Tombstones do not retain claim text. Historical immutable versions provide
    # the safe bridge from the old fingerprint to the canonical one. Refuse an
    # ambiguous truncated-hash remap rather than guessing.
    for old_fp, new_fps in remap.items():
        if len(new_fps) != 1:
            raise StoreError(f'ambiguous legacy fingerprint remap: {old_fp}')
        new_fp = next(iter(new_fps))
        conn.execute(
            'UPDATE tombstone SET claim_fingerprint=? WHERE claim_fingerprint=?',
            (new_fp, old_fp)
        )

    current_rows = conn.execute(
        'SELECT id, current_version_id FROM memory'
    ).fetchall()
    conn.executemany(
        'UPDATE memory SET claim_fingerprint=? WHERE id=?',
        [(canonical_by_version[version_id], memory_id)
         for memory_id, version_id in current_rows]
    )

    # Keep old idempotency keys for replay compatibility and add canonical aliases
    # for the known fingerprint-derived key families used by MemCore/Hermes.
    aliases = []
    for key, project_id, memory_id, version_id, created_at in conn.execute(
        'SELECT key, project_id, memory_id, version_id, created_at FROM idempotency_key'
    ).fetchall():
        if not key.startswith(('remember:', 'observe:', 'import:')):
            continue
        content_row = conn.execute(
            'SELECT content FROM memory_version WHERE id=?', (version_id,)
        ).fetchone()
        if content_row is None:
            continue
        old_fp = _legacy_fingerprint(content_row[0])
        new_fp = _canonical_fingerprint(content_row[0])
        if old_fp != new_fp and key.endswith(':' + old_fp):
            aliases.append((key[:-len(old_fp)] + new_fp,
                            project_id, memory_id, version_id, created_at))
    if aliases:
        conn.executemany(
            'INSERT OR IGNORE INTO idempotency_key '
            '(key, project_id, memory_id, version_id, created_at) VALUES (?, ?, ?, ?, ?)',
            aliases
        )


def _apply_migration(conn, name, sql):
    """Apply one migration and its bookkeeping safely.

    All migrations, including the frozen schema bootstrap, are executed
    statement-by-statement inside one explicit transaction, with bookkeeping
    committed atomically. Table-rebuild migration 0004 temporarily disables FK
    enforcement and runs foreign_key_check before commit, per SQLite guidance.
    """
    already = conn.execute(
        'SELECT 1 FROM schema_migrations WHERE version = ?', (name,)
    ).fetchone()
    if already:
        return

    if sql is None:
        sql = SCHEMA_PATH.read_text(encoding='utf-8')

    rebuild_fk = name == '0004_schema_revision'
    for attempt in range(5):
        if rebuild_fk:
            conn.execute('PRAGMA foreign_keys = OFF')
        try:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute(
                'SELECT 1 FROM schema_migrations WHERE version=?', (name,)
            ).fetchone():
                conn.execute('ROLLBACK')
                return
            if name == '0017_bitemporal_valid_until':
                cols = {r[1] for r in conn.execute(
                    'PRAGMA table_info(memory_version)')}
                if 'valid_until' not in cols:
                    conn.execute(
                        'ALTER TABLE memory_version '
                        'ADD COLUMN valid_until TEXT')
                # Backfill: every superseded version ends when its successor
                # begins. Idempotent — only touches rows still open.
                conn.execute(
                    'UPDATE memory_version SET valid_until = ('
                    '  SELECT s.created_at FROM memory_version s '
                    '  WHERE s.supersedes_version_id = memory_version.id'
                    ') WHERE valid_until IS NULL AND EXISTS ('
                    '  SELECT 1 FROM memory_version s '
                    '  WHERE s.supersedes_version_id = memory_version.id)')
            if name == '0016_scope_detail':
                cols = {r[1] for r in conn.execute(
                    'PRAGMA table_info(memory)')}
                if 'scope_detail' not in cols:
                    conn.execute(
                        'ALTER TABLE memory ADD COLUMN scope_detail TEXT')
            if name == '0015_reinforcement_decay':
                cols = {r[1] for r in conn.execute(
                    'PRAGMA table_info(memory)')}
                if 'recall_count' not in cols:
                    conn.execute(
                        'ALTER TABLE memory '
                        'ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0')
                if 'last_recalled' not in cols:
                    conn.execute(
                        'ALTER TABLE memory ADD COLUMN last_recalled TEXT')
            if name == '0014_provenance_seal':
                cols = {r[1] for r in conn.execute(
                    'PRAGMA table_info(ingest_event)')}
                if 'provenance_seal' not in cols:
                    conn.execute(
                        'ALTER TABLE ingest_event '
                        'ADD COLUMN provenance_seal TEXT')
            if name == '0013_current_version_ownership':
                ownership_violations = _current_version_ownership_violations(conn)
                if ownership_violations:
                    raise StoreError(
                        'current-version ownership violations before migration: '
                        f'{ownership_violations[:5]}'
                    )
            for stmt in _script_statements(sql):
                conn.execute(stmt)
            if name == '0010_performance_fast_paths':
                _backfill_current_fingerprints(conn)
            elif name == '0012_unicode_fingerprint_repair':
                _repair_unicode_fingerprints(conn)
            if rebuild_fk:
                violations = conn.execute('PRAGMA foreign_key_check').fetchall()
                if violations:
                    raise StoreError(
                        f'foreign-key violations after {name}: {violations[:5]}'
                    )
            conn.execute(
                'INSERT INTO schema_migrations (version, applied_at) '
                "VALUES (?, datetime('now'))", (name,)
            )
            conn.execute('COMMIT')
            return
        except sqlite3.OperationalError:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            if attempt == 4:
                raise
            time.sleep(0.2)
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
        finally:
            if rebuild_fk:
                conn.execute('PRAGMA foreign_keys = ON')


def check_migration_lock(conn):
    """Return active or stale crash-visible migration lock info for doctor."""
    try:
        cur = conn.execute(
            'SELECT version, lock_holder, lock_until FROM schema_migrations '
            'WHERE version=? AND lock_holder IS NOT NULL',
            (_MIGRATION_LOCK_VERSION,)
        )
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []
