"""
MemCore — Phase 0 Fixtures.

Seeds an in-memory SQLite DB with:
  2 agents (SORA, MIKA) | 2 projects (memcore, novelclaw)
  memberships | shared + private memories | evidence | tombstone
  stale code-backed fact | conflicting claim pair | rejected claim

Usage:
    import fixtures
    db = fixtures.seed()  # returns sqlite3.Connection (in-memory WAL)
"""
import sqlite3
import uuid
import pathlib
import hashlib
import unicodedata
from datetime import datetime, timedelta

_pkg_schema = pathlib.Path(__file__).resolve().parent.parent / 'memcore' / 'schema' / 'schema.sql'
SCHEMA_PATH = _pkg_schema if _pkg_schema.is_file() else pathlib.Path(__file__).resolve().parent.parent / 'schema' / 'schema.sql'

# ── Canonical IDs ──────────────────────────────────────────────────────
AGENT_SORA   = 'agent-sora'
AGENT_MIKA   = 'agent-mika'

PROJECT_SM   = 'proj-memcore'
PROJECT_NC   = 'proj-novelclaw'

# Agent Smith — the third-party claimant (rejected claim)
AGENT_SMITH  = 'agent-smith'

# Memories in memcore project
MEM_SM_FRONTEND  = 'mem-sm-frontend-framework'
MEM_SM_API       = 'mem-sm-api-contract'
MEM_SM_DEPLOY    = 'mem-sm-deploy-strategy'
MEM_SM_NOTES     = 'mem-sm-sora-private-notes'

# Memories in novelclaw project
MEM_NC_ARCH      = 'mem-nc-architecture-choice'
MEM_NC_AUDIO     = 'mem-nc-audio-stale-fact'
MEM_NC_CONFLICT1 = 'mem-nc-conflict-1'
MEM_NC_CONFLICT2 = 'mem-nc-conflict-2'
MEM_NC_REJECTED  = 'mem-nc-rejected-claim'
MEM_NC_MIKA_PRIV = 'mem-nc-mika-private'

# Versions
VER_SM_FRONTEND_V1 = 'ver-sm-fe-v1'
VER_SM_API_V1      = 'ver-sm-api-v1'
VER_SM_DEPLOY_V1   = 'ver-sm-deploy-v1'
VER_SM_NOTES_V1    = 'ver-sm-notes-v1'
VER_NC_ARCH_V1     = 'ver-nc-arch-v1'
VER_NC_AUDIO_V1    = 'ver-nc-audio-v1'
VER_NC_CONFLICT1_V1 = 'ver-nc-cf1-v1'
VER_NC_CONFLICT2_V1 = 'ver-nc-cf2-v1'
VER_NC_REJECTED_V1  = 'ver-nc-rej-v1'
VER_NC_MIKA_V1      = 'ver-nc-mika-v1'

# Evidence
EVID_SM_BACKEND   = 'evid-sm-backend'
EVID_SM_COMMIT    = 'evid-sm-commit'
EVID_NC_AUDIO     = 'evid-nc-audio'
EVID_NC_REJECTED  = 'evid-nc-rejected'

# Tombstone for the rejected claim
TOMBSTONE_REJECTED = 'tomb-nc-rejected'

# ── Helpers ────────────────────────────────────────────────────────────

def _now():
    return datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')

def _ago(days):
    return (datetime.utcnow() - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')

def _future(days=365):
    return (datetime.utcnow() + timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _fingerprint(text: str) -> str:
    """Deterministic claim fingerprint — same contract as memcore.core."""
    normalized = unicodedata.normalize(
        'NFC', ' '.join(text.lower().strip().split())
    )
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


# ── Schema loader ──────────────────────────────────────────────────────

def _apply_schema(conn: sqlite3.Connection):
    # Run the real migration chain (0001–0005) on this connection instead of
    # raw schema.sql, so fixtures always match what the engine runs (V4 fix).
    from memcore import store as _store
    _store.apply_migrations(conn)


# ── Main fixture seeder ────────────────────────────────────────────────

def seed(db_path: str = ':memory:') -> sqlite3.Connection:
    """
    Return a seeded sqlite3.Connection.

    db_path=':memory:' for unit tests (fast, isolated).
    Pass a real path for concurrency / crash-recovery tests.
    """
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute('PRAGMA journal_mode = WAL')
    conn.execute('PRAGMA busy_timeout = 5000')
    conn.execute('PRAGMA foreign_keys = ON')
    _apply_schema(conn)
    _seed_agents(conn)
    _seed_projects(conn)
    _seed_memberships(conn)
    _seed_evidence(conn)
    _seed_memories(conn)
    # Fixtures insert rows directly, so populate the same indexed identity that
    # engine writes maintain before recall-oriented tests run.
    from memcore import store as _store
    _store._backfill_current_fingerprints(conn)
    _seed_tombstone(conn)
    return conn


# ── Sub-seeders ────────────────────────────────────────────────────────

def _seed_agents(conn):
    conn.executemany(
        'INSERT INTO agent (id, name, profile_key, created_at) VALUES (?, ?, ?, ?)',
        [
            (AGENT_SORA,  'SORA',  'sora',  _ago(30)),
            (AGENT_MIKA,  'MIKA',  'mika',  _ago(30)),
            (AGENT_SMITH, 'Agent Smith', 'smith-third-party', _ago(5)),
        ]
    )


def _seed_projects(conn):
    conn.executemany(
        'INSERT INTO project (id, name, description, created_at) VALUES (?, ?, ?, ?)',
        [
            (PROJECT_SM, 'memcore',
             'The shared memory platform project itself', _ago(20)),
            (PROJECT_NC, 'novelclaw',
             'NovelClaw — Chinese novel translation tool', _ago(25)),
        ]
    )


def _seed_memberships(conn):
    """SORA in memcore; both SORA and MIKA in novelclaw."""
    conn.executemany(
        'INSERT INTO project_membership (project_id, agent_id, role, joined_at) '
        'VALUES (?, ?, ?, ?)',
        [
            (PROJECT_SM, AGENT_SORA, 'owner', _ago(20)),
            (PROJECT_SM, AGENT_MIKA, 'member', _ago(18)),
            (PROJECT_NC, AGENT_SORA, 'member', _ago(25)),
            (PROJECT_NC, AGENT_MIKA, 'owner', _ago(25)),
        ]
    )


def _seed_evidence(conn):
    conn.executemany(
        'INSERT INTO evidence (id, kind, source_uri, source_label, captured_at, verified_at, authority) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        [
            (EVID_SM_BACKEND, 'observation',
             'notes:frontend-framework', 'Team discussion on 2026-08-15',
             _ago(15), _ago(14), 'user_authoritative'),
            (EVID_SM_COMMIT, 'commit',
             'https://github.com/example/commit/abc123', 'commit abc123',
             _ago(10), _ago(10), 'source_backed'),
            (EVID_NC_AUDIO, 'observation',
             'notes:edge-tts-was-working', 'Runtime test 2026-08-20',
             _ago(10), None, 'runtime_verified'),
            (EVID_NC_REJECTED, 'observation',
             'notes:invalid-architecture', 'Agent Smith unverified claim',
             _ago(3), None, 'unverified'),
        ]
    )


def _seed_memories(conn):
    """All memories with their current versions and evidence links."""
    now = _now()

    # ── memcore project — shared memories ─────────────────────────

    # S1: Frontend framework decision (accepted, current, verified)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, pinned, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_SM_FRONTEND, PROJECT_SM, 'project', AGENT_SORA, 'decision',
         'accepted', 'user_authoritative', 'current', VER_SM_FRONTEND_V1,
         1, _ago(15), _ago(15))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, validated_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_SM_FRONTEND_V1, MEM_SM_FRONTEND,
         'Use vanilla HTML/JS for the web UI — no React dependency. '
         'Decision made after evaluating bundle size vs maintainability.',
         'Team consensus on frontend approach',
         AGENT_SORA, _ago(15), _ago(14), _ago(15), None)
    )

    # S2: API contract (accepted, source_backed)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_SM_API, PROJECT_SM, 'project', AGENT_SORA, 'decision',
         'accepted', 'source_backed', 'current', VER_SM_API_V1,
         _ago(10), _ago(10))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, validated_at, valid_from, valid_until, source_commit) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_SM_API_V1, MEM_SM_API,
         'REST API uses /v1/memories with JSON payloads. '
         'All writes require idempotency_key header.',
         'Contract from commit abc123',
         AGENT_SORA, _ago(10), _ago(10), _ago(10), None, 'abc123def')
    )

    # S3: Deploy strategy (accepted, current)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_SM_DEPLOY, PROJECT_SM, 'project', AGENT_MIKA, 'decision',
         'accepted', 'unverified', 'current', VER_SM_DEPLOY_V1,
         _ago(5), _ago(5))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_SM_DEPLOY_V1, MEM_SM_DEPLOY,
         'Deploy memcore as a Hermes plugin (not a standalone daemon). '
         'No process management overhead.',
         'Architecture decision',
         AGENT_MIKA, _ago(5), _ago(5), None)
    )

    # S4: SORA's private notes (private, accepted)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_SM_NOTES, PROJECT_SM, 'private', AGENT_SORA, 'note',
         'accepted', 'unverified', 'current', VER_SM_NOTES_V1,
         _ago(3), _ago(3))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_SM_NOTES_V1, MEM_SM_NOTES,
         'SORA: Schema looks good but need to decide on FTS5 tokenizer. '
         'Leaning toward unicode61 for multilingual support.',
         'Personal research note',
         AGENT_SORA, _ago(3), _ago(3), None)
    )

    # ── novelclaw project — shared memories ─────────────────────────────

    # N1: Architecture choice (accepted, current)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_NC_ARCH, PROJECT_NC, 'project', AGENT_MIKA, 'decision',
         'accepted', 'user_authoritative', 'current', VER_NC_ARCH_V1,
         _ago(20), _ago(20))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, validated_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_NC_ARCH_V1, MEM_NC_ARCH,
         'NovelClaw uses Go with embedded HTML/JS frontend. '
         'HTTP server on configurable port (default 4890).',
         'Original architecture decision',
         AGENT_MIKA, _ago(20), _ago(20), _ago(20), None)
    )

    # N2: Stale code-backed fact — edge-tts audio
    # The source hash represents an earlier version of a real repo file.
    # Absolute paths keep detection independent of the caller's working directory.
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_NC_AUDIO, PROJECT_NC, 'project', AGENT_SORA, 'fact',
         'accepted', 'source_backed', 'stale', VER_NC_AUDIO_V1,
         _ago(10), _ago(10))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, validated_at, valid_from, valid_until, '
        '   source_hash, source_commit, source_file) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_NC_AUDIO_V1, MEM_NC_AUDIO,
         'Edge-TTS library version 0.1.0 is installed and working for '
         'Chinese voice synthesis in audio preview.',
         'Verified during audio feature development',
         AGENT_SORA, _ago(10), _ago(10), _ago(10), None,
         hashlib.sha256(b'previous pyproject contents').hexdigest(),
         'old-commit-hash-001',
         str(pathlib.Path(__file__).resolve().parent.parent / 'pyproject.toml'))
    )

    # ── novelclaw project — conflicting claim pair ──────────────────────

    # N3a: Conflict claim 1 (from SORA)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_NC_CONFLICT1, PROJECT_NC, 'project', AGENT_SORA, 'fact',
         'conflict', 'unverified', 'current', VER_NC_CONFLICT1_V1,
         _ago(7), _ago(7))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_NC_CONFLICT1_V1, MEM_NC_CONFLICT1,
         'The novel data directory should be ./novels relative to the executable.',
         'Observation from directory structure inspection',
         AGENT_SORA, _ago(7), _ago(7), None)
    )

    # N3b: Conflict claim 2 (from MIKA — contradicts SORA's claim)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_NC_CONFLICT2, PROJECT_NC, 'project', AGENT_MIKA, 'fact',
         'conflict', 'unverified', 'current', VER_NC_CONFLICT2_V1,
         _ago(6), _ago(6))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_NC_CONFLICT2_V1, MEM_NC_CONFLICT2,
         'The data directory was changed to ./data in the latest refactor.',
         'Observation from config.json inspection',
         AGENT_MIKA, _ago(6), _ago(6), None)
    )

    # ── novelclaw project — rejected claim with tombstone ───────────────

    # N4: Rejected claim (by third-party agent, now rejected + tombstoned)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_NC_REJECTED, PROJECT_NC, 'project', AGENT_SMITH, 'fact',
         'rejected', 'unverified', 'current', VER_NC_REJECTED_V1,
         _ago(2), _ago(2))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_NC_REJECTED_V1, MEM_NC_REJECTED,
         'NovelClaw uses Docker for deployment with a PostgreSQL database backend.',
         'Unverified architectural claim',
         AGENT_SMITH, _ago(2), _ago(2), None)
    )

    # N5: MIKA's private memory (private, should not be visible to SORA)
    conn.execute(
        'INSERT INTO memory (id, project_id, scope, owner_agent_id, type, '
        '  lifecycle, verification, freshness, current_version_id, '
        '  created_at, updated_at) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (MEM_NC_MIKA_PRIV, PROJECT_NC, 'private', AGENT_MIKA, 'note',
         'accepted', 'unverified', 'current', VER_NC_MIKA_V1,
         _ago(12), _ago(12))
    )
    conn.execute(
        'INSERT INTO memory_version '
        '  (id, memory_id, content, reason, created_by_agent_id, '
        '   created_at, valid_from, valid_until) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (VER_NC_MIKA_V1, MEM_NC_MIKA_PRIV,
         'MIKA: Consider switching from edge-tts to Azure TTS for better '
         'voice quality. Budget approval needed.',
         'Private planning note',
         AGENT_MIKA, _ago(12), _ago(12), None)
    )

    # ── Evidence links ──────────────────────────────────────────────────
    conn.executemany(
        'INSERT INTO evidence_link (evidence_id, memory_version_id, relation) '
        'VALUES (?, ?, ?)',
        [
            (EVID_SM_BACKEND, VER_SM_FRONTEND_V1, 'supports'),
            (EVID_SM_COMMIT,  VER_SM_API_V1,      'supports'),
            (EVID_NC_AUDIO,   VER_NC_AUDIO_V1,     'supports'),
            (EVID_NC_REJECTED, VER_NC_REJECTED_V1,  'contradicts'),
        ]
    )

    # ── Audit trail ─────────────────────────────────────────────────────
    conn.executemany(
        'INSERT INTO audit_event (action, actor_agent_id, memory_id, project_id, detail, created_at) '
        'VALUES (?, ?, ?, ?, ?, ?)',
        [
            ('create', AGENT_SORA, MEM_SM_FRONTEND, PROJECT_SM,
             '{"reason": "frontend framework decision"}', _ago(15)),
            ('promote', None, MEM_SM_FRONTEND, PROJECT_SM,
             '{"lifecycle": "accepted"}', _ago(14)),
            ('create', AGENT_MIKA, MEM_NC_CONFLICT2, PROJECT_NC,
             '{"reason": "data directory observation"}', _ago(6)),
            ('reject', AGENT_MIKA, MEM_NC_REJECTED, PROJECT_NC,
             '{"reason": "Docker claim contradicted by actual config"}', _ago(1)),
        ]
    )

    conn.commit()
    return conn


def _seed_tombstone(conn):
    """Insert tombstone for the rejected claim — blocks resurrection."""
    # The fingerprint is computed from the content of the rejected claim
    content = (
        'NovelClaw uses Docker for deployment with a PostgreSQL database backend.'
    )
    fp = _fingerprint(content)
    conn.execute(
        'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
        'VALUES (?, ?, ?, ?, ?)',
        (TOMBSTONE_REJECTED, fp, PROJECT_NC,
         'Claim contradicted by config.json — NovelClaw is a Go binary with '
         'no Docker or PostgreSQL dependency.',
         _ago(1))
    )
    conn.commit()
    return fp  # return so tests can use it


# ── Convenience: db_path on disk for concurrency / crash tests ──────────

def temp_db_path(tmp_dir: str) -> str:
    """Return a real file path inside tmp_dir for WAL-mode tests."""
    import os
    return os.path.join(tmp_dir, 'shared_memory_test.db')
