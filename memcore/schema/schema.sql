-- MemCore — Minimal SQLite Schema (Phase 0 contract)
-- WAL mode, FTS5, bitemporal MemoryVersion, Tombstone entity, 3-axis status.
-- Phase 1 MUST implement exactly this contract.
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS project (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    profile_key TEXT NOT NULL UNIQUE,
    metadata    TEXT DEFAULT '{}',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS project_membership (
    project_id  TEXT NOT NULL REFERENCES project(id),
    agent_id    TEXT NOT NULL REFERENCES agent(id),
    role        TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('member', 'owner')),
    joined_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, agent_id)
);

CREATE TABLE IF NOT EXISTS memory (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES project(id),
    scope               TEXT NOT NULL CHECK (scope IN ('project', 'private')),
    owner_agent_id      TEXT NOT NULL REFERENCES agent(id),
    type                TEXT NOT NULL DEFAULT 'fact',
    -- 3-axis status model (Finding A)
    lifecycle           TEXT NOT NULL DEFAULT 'candidate'
                            CHECK (lifecycle IN ('candidate', 'accepted', 'conflict', 'superseded', 'rejected', 'disabled')),
    verification        TEXT NOT NULL DEFAULT 'unverified'
                            CHECK (verification IN ('unverified', 'source_backed', 'runtime_verified', 'user_authoritative')),
    freshness           TEXT NOT NULL DEFAULT 'current'
                            CHECK (freshness IN ('current', 'aging', 'stale')),
    current_version_id  TEXT,
    pinned              INTEGER NOT NULL DEFAULT 0,
    critical            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_version (
    id                      TEXT PRIMARY KEY,
    memory_id               TEXT NOT NULL REFERENCES memory(id),
    content                 TEXT NOT NULL,
    reason                  TEXT,
    created_by_agent_id     TEXT NOT NULL REFERENCES agent(id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    validated_at            TEXT,
    -- bitemporal: world-validity time (Finding D)
    valid_from              TEXT NOT NULL DEFAULT (datetime('now')),
    valid_until             TEXT,
    supersedes_version_id   TEXT REFERENCES memory_version(id),
    source_hash             TEXT,
    source_commit           TEXT,
    source_file             TEXT
);

-- Phase 1 adds: FK constraint memory.current_version_id -> memory_version.id
-- (deferred until engine exists to avoid circular insert issues)

CREATE TABLE IF NOT EXISTS evidence (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('file', 'commit', 'test', 'observation', 'user_input', 'external')),
    source_uri  TEXT,
    source_label TEXT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    verified_at TEXT,
    authority   TEXT NOT NULL DEFAULT 'unverified'
                    CHECK (authority IN ('unverified', 'source_backed', 'runtime_verified', 'user_authoritative')),
    metadata    TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS evidence_link (
    evidence_id     TEXT NOT NULL REFERENCES evidence(id),
    memory_version_id TEXT NOT NULL REFERENCES memory_version(id),
    relation        TEXT NOT NULL CHECK (relation IN ('supports', 'contradicts', 'supersedes', 'context_for')),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (evidence_id, memory_version_id)
);

CREATE TABLE IF NOT EXISTS audit_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action          TEXT NOT NULL,
    actor_agent_id  TEXT REFERENCES agent(id),
    memory_id       TEXT REFERENCES memory(id),
    project_id      TEXT REFERENCES project(id),
    detail          TEXT DEFAULT '{}',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tombstone (
    id                  TEXT PRIMARY KEY,
    claim_fingerprint   TEXT NOT NULL,
    scope               TEXT NOT NULL,  -- project_id or 'global'
    reason              TEXT NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    overridden_by       TEXT  -- NULL = active; set to agent_id to override
);

-- FTS5 retrieval index over memory_version content (external-content, rebuildable).
-- Phase 1 adds sync triggers (INSERT/UPDATE/DELETE) keeping this in step with memory_version.
CREATE VIRTUAL TABLE IF NOT EXISTS memory_version_fts USING fts5(
    content,
    content='memory_version',
    content_rowid='rowid'
);
-- Bulk rebuild: INSERT INTO memory_version_fts(memory_version_fts) VALUES('rebuild');

-- Indexes for retrieval path
-- NOTE: post-contract changes (FK constraints, audit write_key, timestamp
-- guards) live in migrations 0004+ in memcore/store.py — this file stays frozen.
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
