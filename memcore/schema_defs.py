"""
MemCore — frozen SQL migration bodies.

Pure DDL string literals only: no imports, no runtime logic. `store.py` builds
the ordered MIGRATIONS registry from these and owns their execution.
"""

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

_MEMORY_TRIPLES = """
-- 0019: governed entity-relation knowledge triples linked to canonical memories.
CREATE TABLE IF NOT EXISTS memory_triple (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    version_id  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (memory_id) REFERENCES memory(id) ON DELETE CASCADE,
    FOREIGN KEY (version_id) REFERENCES memory_version(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_triple_sub_pred ON memory_triple(subject, predicate);
CREATE INDEX IF NOT EXISTS idx_triple_obj ON memory_triple(object);
CREATE INDEX IF NOT EXISTS idx_triple_memory ON memory_triple(memory_id);
CREATE INDEX IF NOT EXISTS idx_triple_version ON memory_triple(version_id);
"""

_BINARY_VECTOR_COLUMN = """
-- 0020: add 1-bit binary vector column for microsecond two-stage vector pruning
ALTER TABLE memory_embedding ADD COLUMN binary_vector BLOB;
"""

