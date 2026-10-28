"""
MemCore — storage layer.

open_store(): apply schema, run pending migrations under a lock, set pragmas.
All operations use short transactions and WAL + busy_timeout.
"""
import contextlib
import hashlib
import math
import os
import pathlib
import random
import sqlite3
import time
import unicodedata

from .schema_defs import _BITEMPORAL_VALID_UNTIL
from .schema_defs import _CURRENT_VERSION_OWNERSHIP
from .schema_defs import _FTS_TRIGGERS
from .schema_defs import _IDEMPOTENCY
from .schema_defs import _INGEST_JOURNAL
from .schema_defs import _INTEGRITY_HARDENING
from .schema_defs import _ISO_NORMALIZE
from .schema_defs import _MEMORY_EMBEDDING
from .schema_defs import _MEMORY_TRIPLES
from .schema_defs import _BINARY_VECTOR_COLUMN
from .schema_defs import _PERFORMANCE_FAST_PATHS
from .schema_defs import _PERFORMANCE_ROUND2
from .schema_defs import _PROVENANCE_SEAL
from .schema_defs import _REINFORCEMENT_DECAY
from .schema_defs import _SCHEMA_REVISION
from .schema_defs import _SCOPE_DETAIL
from .schema_defs import _SEMANTIC_ANALYSIS
from .schema_defs import _UNICODE_FINGERPRINT_REPAIR

from .store_backup import (
    verify_backups,
)

from .store_errors import (
    StoreError,
)

from .store_seals import (
    verify_all_seals,
)

from .store_backup import (
    BACKUP_DIRNAME,
    BACKUP_MAX_AGE_DAYS,
    BACKUP_MIN_COUNT,
    _SNAPSHOT_RE,
    _is_managed_snapshot,
    _managed_snapshots,
    backup_store,
    _prune_backups,
    verify_backups,
)

from .store_errors import (
    StoreError,
)

from .store_seals import (
    corroboration_funnel,
    _active_provenance_key,
    seal_event,
    verify_event_seal,
    verify_all_seals,
)

from .store_vectors import (
    pack_vector,
    unpack_vector,
    store_embedding,
    get_embedding,
)




def apply_wal_pragmas(conn: sqlite3.Connection, busy_timeout_ms: int = 15000) -> None:
    """Apply resilient Windows 11 WAL pragmas across Trinity stores."""
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA journal_size_limit = 8388608")
    conn.execute("PRAGMA wal_autocheckpoint = 200")



def attach_trinity_spokes(
    conn: sqlite3.Connection,
    skills_db: str | None = None,
    blobs_db: str | None = None,
) -> list[str]:
    """Idempotently ATTACH sibling databases (skills.db, blobs.db) in read-only mode."""
    attached = []
    current_dbs = {row[1] for row in conn.execute("PRAGMA database_list").fetchall()}
    targets = [
        ("skills_spoke", skills_db or str(pathlib.Path.home() / ".trinity" / "skills.db")),
        ("blobs_spoke", blobs_db or str(pathlib.Path.home() / ".trinity" / "blobs.db")),
    ]
    for schema_name, db_file in targets:
        if schema_name in current_dbs:
            attached.append(schema_name)
            continue
        p = pathlib.Path(db_file).expanduser()
        if p.is_file():
            try:
                conn.execute(f"ATTACH DATABASE ? AS {schema_name}", (str(p),))
                attached.append(schema_name)
            except Exception:
                pass
    return attached



def _register_math_functions(conn: sqlite3.Connection) -> None:
    """Ensure SQLite math functions (exp, ln, log, sqrt, pow) are available even when SQLite was built without SQLITE_ENABLE_MATH_FUNCTIONS."""
    try:
        conn.create_function(
            "exp", 1, lambda x: math.exp(float(x)) if x is not None else None, deterministic=True
        )
        conn.create_function(
            "ln", 1, lambda x: math.log(float(x)) if x is not None and float(x) > 0 else None, deterministic=True
        )
        conn.create_function(
            "log", 1, lambda x: math.log(float(x)) if x is not None and float(x) > 0 else None, deterministic=True
        )
        conn.create_function(
            "log10", 1, lambda x: math.log10(float(x)) if x is not None and float(x) > 0 else None, deterministic=True
        )
        conn.create_function(
            "sqrt", 1, lambda x: math.sqrt(float(x)) if x is not None and float(x) >= 0 else None, deterministic=True
        )
        conn.create_function(
            "pow", 2, lambda x, y: math.pow(float(x), float(y)) if x is not None and y is not None else None, deterministic=True
        )
        conn.create_function(
            "power", 2, lambda x, y: math.pow(float(x), float(y)) if x is not None and y is not None else None, deterministic=True
        )
    except Exception:
        pass



def _register_vector_functions(conn: sqlite3.Connection) -> None:
    """Load sqlite-vec extension if available, enabling in-process SIMD vector distance operations."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            try:
                conn.enable_load_extension(False)
            except Exception:
                pass
    except Exception:
        pass



def _resolve_schema_path() -> pathlib.Path:
    # 1. Inside package (installed wheel / site-packages)
    pkg_schema = pathlib.Path(__file__).resolve().parent / 'schema' / 'schema.sql'
    if pkg_schema.is_file():
        return pkg_schema
    # 2. Repo root parent/schema/schema.sql
    repo_schema = pathlib.Path(__file__).resolve().parent.parent / 'schema' / 'schema.sql'
    if repo_schema.is_file():
        return repo_schema
    return pkg_schema



SCHEMA_PATH = _resolve_schema_path()

_MIGRATION_LOCK_VERSION = '__migration_lock__'

_MIGRATION_LOCK_TTL_SECONDS = 30.0

_MIGRATION_LOCK_WAIT_SECONDS = 10.0



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
    ('0019_memory_triples', _MEMORY_TRIPLES),
    ('0020_binary_vector', _BINARY_VECTOR_COLUMN),
]



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
    if p.is_dir():
        raise StoreError(f'store path is a directory, not a database: {p}')
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
    try:
        conn = sqlite3.connect(
            str(p), timeout=10, isolation_level=None,
            check_same_thread=check_same_thread
        )
    except Exception as e:
        if isinstance(e, StoreError):
            raise
        raise StoreError(f'failed to open store at {p}: {e}') from e
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
        _register_math_functions(conn)
        _register_vector_functions(conn)
        try:
            apply_migrations(conn)
        except sqlite3.OperationalError as e:
            if 'readonly' in str(e).lower() or 'read-only' in str(e).lower():
                raise StoreError(f'read-only store needs migration but cannot write: {e}') from e
            raise
        return conn
    except sqlite3.OperationalError:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        if isinstance(e, StoreError):
            raise
        raise StoreError(f'failed to initialize store at {p}: {e}') from e



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
        raise StoreError(f'store does not exist or is not a file: {p}')
    mode = 'ro' if readonly else 'rw'
    try:
        conn = sqlite3.connect(
            p.as_uri() + f'?mode={mode}', uri=True, timeout=10,
            isolation_level=None, check_same_thread=check_same_thread
        )
    except Exception as e:
        if isinstance(e, StoreError):
            raise
        raise StoreError(f'failed to open store at {p}: {e}') from e
    try:
        conn.execute('PRAGMA busy_timeout = 5000')
        conn.execute('PRAGMA foreign_keys = ON')
        if readonly:
            conn.execute('PRAGMA query_only = ON')
        else:
            conn.execute('PRAGMA synchronous = NORMAL')
        _register_math_functions(conn)
        _register_vector_functions(conn)
        # Validate that the file is indeed a readable sqlite database
        conn.execute('PRAGMA schema_version')
        return conn
    except Exception as e:
        conn.close()
        if isinstance(e, StoreError):
            raise
        raise StoreError(f'failed to initialize store connection at {p}: {e}') from e



def open_runtime_store(db_path: str, check_same_thread: bool = True) -> sqlite3.Connection:
    """Fast writable opener; requires a store already bootstrapped by ``open_store``."""
    return _open_existing_connection(
        db_path, readonly=False, check_same_thread=check_same_thread
    )



def open_runtime_store_readonly(db_path: str) -> sqlite3.Connection:
    """Fast read-only opener for a store validated during provider initialization."""
    return _open_existing_connection(db_path, readonly=True)



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
    except Exception as e:
        conn.close()
        if isinstance(e, StoreError):
            raise
        raise StoreError(f'failed to validate read-only store: {e}') from e



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
    v = _current_version(conn)
    known = [ver for ver, _ in MIGRATIONS]
    if '0018_memory_embedding' in known and v in known and known.index(v) >= known.index('0018_memory_embedding'):
        _ensure_column(conn, 'memory_version', 'when_to_use', 'TEXT')
    _invalidate_core_schema_caches()



def _invalidate_core_schema_caches() -> None:
    """H4: drop core's per-connection table-columns cache after migrations.

    Lazy import keeps store importable without core during bootstrap; any
    failure is non-fatal (the cache self-heals via its size cap and the
    no-empty-results rule).
    """
    try:
        from . import core as _core
        _core._clear_table_cols_cache()
    except Exception:
        pass



@contextlib.contextmanager
def write_tx(conn: sqlite3.Connection, manage: bool = True, max_retries: int = 5):
    """Deep write-transaction lifetime: BEGIN IMMEDIATE / COMMIT / ROLLBACK with jittered retry and re-entrancy."""
    if not manage:
        yield
        return

    # Re-entrant: if already in a transaction, use a savepoint
    if getattr(conn, "in_transaction", False):
        sp_name = f"tx_sp_{random.randint(1000, 9999)}"
        conn.execute(f"SAVEPOINT {sp_name}")
        try:
            yield
            conn.execute(f"RELEASE {sp_name}")
        except Exception:
            try:
                conn.execute(f"ROLLBACK TO {sp_name}")
                conn.execute(f"RELEASE {sp_name}")
            except sqlite3.OperationalError:
                pass
            raise
        return

    # Begin immediate with jittered backoff on locked database
    for attempt in range(max(1, max_retries)):
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                time.sleep(random.uniform(0.005, 0.040) * (attempt + 1))
                continue
            raise

    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise



@contextlib.contextmanager
def _savepoint(conn, name: str):
    """Per-item atomicity inside a caller-owned transaction.

    Used by batch writers (import_memories) so one item's failure rolls
    back only that item: RELEASE on success, ROLLBACK TO + RELEASE on
    error, preserving the original exception either way.
    """
    safe_name = ''.join(c for c in str(name) if c.isalnum() or c == '_') or 'sp'
    conn.execute(f'SAVEPOINT {safe_name}')
    try:
        yield
        conn.execute(f'RELEASE {safe_name}')
    except Exception:
        try:
            conn.execute(f'ROLLBACK TO {safe_name}')
            conn.execute(f'RELEASE {safe_name}')
        except sqlite3.OperationalError:
            pass
        raise



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



def _ensure_column(conn, table: str, col: str, col_def: str) -> None:
    """Idempotent ALTER TABLE ADD COLUMN (SQLite has no IF NOT EXISTS here)."""
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
    if col not in cols:
        try:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {col} {col_def}')
        except sqlite3.OperationalError as exc:
            if 'duplicate column' not in str(exc).lower():
                raise



def _pre_0013_ownership_guard(conn):
    """Refuse 0013 while rows still violate current-version ownership."""
    violations = _current_version_ownership_violations(conn)
    if violations:
        raise StoreError(
            'current-version ownership violations before migration: '
            f'{violations[:5]}'
        )



def _pre_0015_reinforcement(conn):
    _ensure_column(conn, 'memory', 'recall_count',
                   'INTEGER NOT NULL DEFAULT 0')
    _ensure_column(conn, 'memory', 'last_recalled', 'TEXT')



def _pre_0017_bitemporal(conn):
    _ensure_column(conn, 'memory_version', 'valid_until', 'TEXT')
    # Backfill: every superseded version ends when its successor begins.
    # Idempotent — only touches rows still open.
    conn.execute(
        'UPDATE memory_version SET valid_until = ('
        '  SELECT s.created_at FROM memory_version s '
        '  WHERE s.supersedes_version_id = memory_version.id'
        ') WHERE valid_until IS NULL AND EXISTS ('
        '  SELECT 1 FROM memory_version s '
        '  WHERE s.supersedes_version_id = memory_version.id)')



#: Declarative per-migration Python steps. Pre-hooks run inside the migration
#: transaction before its SQL (ADD COLUMN is not idempotent in SQL, so the
#: ALTERs live here behind _ensure_column); post-hooks run after the SQL and
#: before bookkeeping. The retry loop in _apply_migration stays generic.
_PRE_MIGRATION_HOOKS = {
    '0013_current_version_ownership': _pre_0013_ownership_guard,
    '0014_provenance_seal': lambda conn: _ensure_column(
        conn, 'ingest_event', 'provenance_seal', 'TEXT'),
    '0015_reinforcement_decay': _pre_0015_reinforcement,
    '0016_scope_detail': lambda conn: _ensure_column(
        conn, 'memory', 'scope_detail', 'TEXT'),
    '0017_bitemporal_valid_until': _pre_0017_bitemporal,
}

_POST_MIGRATION_HOOKS = {
    '0010_performance_fast_paths': _backfill_current_fingerprints,
    '0012_unicode_fingerprint_repair': _repair_unicode_fingerprints,
}



def _run_migration_body(conn, name, sql, rebuild_fk: bool) -> None:
    """One migration attempt in its own explicit transaction.

    Commits bookkeeping atomically with the migration body. Table-rebuild
    migration 0004 temporarily disables FK enforcement and runs
    foreign_key_check before commit, per SQLite guidance.
    """
    if rebuild_fk:
        conn.execute('PRAGMA foreign_keys = OFF')
    try:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute(
            'SELECT 1 FROM schema_migrations WHERE version=?', (name,)
        ).fetchone():
            conn.execute('ROLLBACK')
            return
        pre_hook = _PRE_MIGRATION_HOOKS.get(name)
        if pre_hook is not None:
            pre_hook(conn)
        for stmt in _script_statements(sql):
            conn.execute(stmt)
        post_hook = _POST_MIGRATION_HOOKS.get(name)
        if post_hook is not None:
            post_hook(conn)
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
    except sqlite3.OperationalError:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        if rebuild_fk:
            conn.execute('PRAGMA foreign_keys = ON')



def _apply_migration(conn, name, sql):
    """Apply one migration under a bounded retry loop.

    Bookkeeping and body are committed atomically by _run_migration_body;
    OperationalError retries up to 5 times with a short backoff, any other
    failure propagates immediately.
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
        try:
            _run_migration_body(conn, name, sql, rebuild_fk)
            return
        except sqlite3.OperationalError:
            if attempt == 4:
                raise
            time.sleep(0.2)



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



def _fts_sync_report(conn) -> dict:
    """FTS rowid count vs version count; in_sync is the health contract."""
    fts_rows = conn.execute(
        'SELECT COUNT(*) FROM memory_version_fts').fetchone()[0]
    ver_rows = conn.execute(
        'SELECT COUNT(*) FROM memory_version').fetchone()[0]
    return {'fts_rows': fts_rows, 'version_rows': ver_rows,
            'in_sync': fts_rows == ver_rows}



def _orphan_counts(conn) -> dict:
    """Referential integrity census: rows whose parents vanished."""
    return {
        'orphaned_memory_versions': conn.execute(
            'SELECT COUNT(*) FROM memory_version v '
            'LEFT JOIN memory m ON m.id = v.memory_id WHERE m.id IS NULL'
        ).fetchone()[0],
        'orphaned_audit_events': conn.execute(
            'SELECT COUNT(*) FROM audit_event a '
            'LEFT JOIN memory m ON m.id = a.memory_id '
            'LEFT JOIN project p ON p.id = a.project_id '
            'WHERE (a.memory_id IS NOT NULL AND m.id IS NULL) '
            '   OR (a.project_id IS NOT NULL AND p.id IS NULL)'
        ).fetchone()[0],
    }



def _idempotency_violation_count(conn) -> int:
    """idempotency_key predates FKs: validate refs and cross-row identity."""
    return conn.execute(
        'SELECT COUNT(*) FROM idempotency_key ik '
        'LEFT JOIN project p ON p.id=ik.project_id '
        'LEFT JOIN memory m ON m.id=ik.memory_id '
        'LEFT JOIN memory_version v ON v.id=ik.version_id '
        'WHERE ik.project_id IS NULL OR p.id IS NULL '
        '   OR m.id IS NULL OR v.id IS NULL '
        '   OR (v.id IS NOT NULL AND v.memory_id != ik.memory_id) '
        '   OR (m.id IS NOT NULL AND ik.project_id IS NOT NULL '
        '       AND m.project_id != ik.project_id)'
    ).fetchone()[0]



def inspect_storage_health(conn, db_path) -> dict:
    """Storage-layer health report for one open store (doctor's deep seam).

    Structural/file concerns only: SQLite integrity, referential orphans,
    FTS sync, migration locks, backup readiness, provenance seals. Governance
    checks (claim fingerprints, tombstones, memberships) live in
    ``core.inspect_governance_health``. Read-only; never mutates; the CLI
    adapter decides which findings gate the exit status.
    """
    path = pathlib.Path(str(db_path)).expanduser()
    return {
        'integrity_check': conn.execute(
            'PRAGMA integrity_check').fetchone()[0],
        'foreign_key_violations': len(
            conn.execute('PRAGMA foreign_key_check').fetchall()),
        'journal_mode': conn.execute(
            'PRAGMA journal_mode').fetchone()[0].lower(),
        'wal_file': {
            'exists': path.with_suffix(path.suffix + '-wal').exists(),
            'shm_file': path.with_suffix(path.suffix + '-shm').exists(),
        },
        **_orphan_counts(conn),
        'idempotency_violations': _idempotency_violation_count(conn),
        'current_version_ownership_violations':
            _current_version_ownership_violations(conn),
        'fts_index': _fts_sync_report(conn),
        'migration_locks': check_migration_lock(conn) or 'none',
        'backups': verify_backups(str(db_path)),
        'provenance': verify_all_seals(conn),
    }
