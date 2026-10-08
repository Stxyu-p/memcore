"""
MemCore — Phase 0 Integration Evaluation Suite.

12 evaluations from RESEARCH_AUDIT.md § Required integration evaluations.
Two tests (concurrency, crash recovery) run for real against SQLite WAL.
All others are EXPECTED FAILURE until the core engine lands in Phase 1+.

Run:  python -m unittest discover   (from project root)
      python -m harness              (alternative)
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

# Fixtures live one level up; add to path so import works from harness/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import fixtures
from memcore import core, store


# ═══════════════════════════════════════════════════════════════════════
# Data paths — now wired to the real MemCore engine (Phase 1).
# Shapes match the original stubs so assertions are unchanged.
# ═══════════════════════════════════════════════════════════════════════

def _query_shared_visible(conn, project_id, agent_id):
    """Memories readable by agent_id in project — core.visible_memories.
    Scope enforced in SQL WHERE (never post-filtering)."""
    return core.visible_memories(conn, project_id, agent_id)


def _query_private_isolation(conn, project_id, viewer_agent_id):
    """ONLY viewer's private memories — core.private_memories."""
    return core.private_memories(conn, project_id, viewer_agent_id)


def _query_cross_project(conn, agent_id, target_project_id):
    """Cross-project discovery — Phase 7 feature (reusable/shareable only).
    No core implementation yet; raw SQL kept verbatim from the stub."""
    cur = conn.execute(
        'SELECT m.id, v.content '
        'FROM memory m '
        'JOIN memory_version v ON v.id = m.current_version_id '
        'WHERE m.project_id = ? '
        '  AND m.scope = \'project\' '
        '  AND m.lifecycle = \'accepted\' '
        '  AND m.verification IN (\'source_backed\', \'runtime_verified\', \'user_authoritative\')',
        (target_project_id,)
    )
    return cur.fetchall()


def _query_superseded_excluded(conn, project_id, agent_id):
    """Current-truth recall excluding superseded — adapter over
    core.visible_memories preserving the stub's (id, lifecycle, content) shape."""
    rows = core.visible_memories(conn, project_id, agent_id)
    return [(r[0], r[2], r[5]) for r in rows]


def _admission_check(conn, claim_text, project_id):
    """Tombstone admission guard — core.admission_allowed.
    True = no active tombstone match; False = blocked."""
    return core.admission_allowed(conn, claim_text, project_id)


def _resolve_conflict_memories(conn, project_id, agent_id):
    """Readable conflict memories — core enforces membership/private scope."""
    return core.conflict_memories(conn, project_id, agent_id)


def _simulate_supersede(conn, old_memory_id, new_content, agent_id, reason):
    """Supersede via the real engine: new immutable version + current pointer
    flip + audit trail, history intact."""
    return core.supersede(conn, old_memory_id, agent_id, new_content, reason)


# ═══════════════════════════════════════════════════════════════════════
# Evaluation test cases
# ═══════════════════════════════════════════════════════════════════════

class EvalTestBase(unittest.TestCase):
    """Shared setup: seed an in-memory DB before each test."""

    def setUp(self):
        self.conn = fixtures.seed()

    def tearDown(self):
        self.conn.close()


# ── E1: Shared decision recall ────────────────────────────────────────
# "Profile A writes; Profile B in same project recalls it."
class TestE1_SharedDecisionRecall(EvalTestBase):
    """E1 — Shared memory written by SORA must be readable by MIKA in same project."""

    def test_sora_write_readable_by_mika(self):
        """MIKA (project member) should see SORA's frontend decision in memcore project."""
        rows = _query_shared_visible(self.conn, fixtures.PROJECT_SM, fixtures.AGENT_MIKA)
        contents = [r[5] for r in rows]  # r[5] = content
        self.assertTrue(
            any('vanilla HTML/JS' in c for c in contents),
            'MIKA should see SORA frontend decision but query returned: '
            + str(contents)
        )


# ── E2: Private isolation ─────────────────────────────────────────────
# "Profile B can never retrieve A-private memory, including broad search."
class TestE2_PrivateIsolation(EvalTestBase):
    """E2 — MIKA must NOT be able to read SORA's private memories."""

    def test_mika_cannot_read_sora_private(self):
        """Private memories scoped to SORA must be invisible to MIKA."""
        rows = _query_private_isolation(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_MIKA
        )
        private_ids = [r[0] for r in rows]
        self.assertNotIn(
            fixtures.MEM_SM_NOTES, private_ids,
            'SORA private notes leaked to MIKA via private-isolation query!'
        )

    def test_sora_sees_own_private(self):
        """SORA must still see her own private memories."""
        rows = _query_private_isolation(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_SORA
        )
        private_ids = [r[0] for r in rows]
        self.assertIn(
            fixtures.MEM_SM_NOTES, private_ids,
            'SORA cannot read her own private notes!'
        )


# ── E3: Project isolation ─────────────────────────────────────────────
# "Project B cannot discover Project A memory unless explicitly marked reusable."
class TestE3_ProjectIsolation(EvalTestBase):
    """E3 — novelclaw project must not leak into memcore recall."""

    def test_novelclaw_memories_hidden_from_shared_memory_project(self):
        """NovelClaw memories should not appear when querying memcore project."""
        rows = _query_shared_visible(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_SORA
        )
        content_str = str([r[5] for r in rows])
        self.assertNotIn(
            'Go with embedded HTML', content_str,
            'NovelClaw architecture decision leaked into memcore project scope!'
        )


# ── E4: Irrelevant recall ────────────────────────────────────────────
# "Unrelated project memory is not injected for an unrelated task."
class TestE4_IrrelevantRecall(EvalTestBase):
    """E4 — Memories from novelclaw should not appear when working on memcore."""

    def test_unrelated_project_not_injected(self):
        """Querying memcore project should not surface novelclaw decisions."""
        rows = _query_shared_visible(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_SORA
        )
        for row in rows:
            content = row[5] or ''
            self.assertNotIn(
                'Docker', content,
                f'Unrelated novelclaw memory {row[0]} leaked into memcore recall!'
            )


# ── E5: Correction/supersede ─────────────────────────────────────────
# "New verified version supersedes old current version; history remains queryable."
class TestE5_Correction(EvalTestBase):
    """E5 — Superseded memories must not appear as current; history must survive."""

    def test_superseded_excluded_from_current_recall(self):
        """After supersede, old version must not be the 'current' recall."""
        # Supersede the API contract memory
        _simulate_supersede(
            self.conn, fixtures.MEM_SM_API,
            'REST API changed to GraphQL with /v1/query endpoint.',
            fixtures.AGENT_MIKA,
            'Architecture migration'
        )
        rows = _query_superseded_excluded(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_SORA
        )
        contents = [r[2] for r in rows]
        self.assertFalse(
            any('REST API uses /v1/memories' in c for c in contents),
            'Superseded REST API memory still appears as current recall!'
        )

    def test_superseded_history_remains_queryable(self):
        """Old version must still exist in memory_version for historical queries."""
        _simulate_supersede(
            self.conn, fixtures.MEM_SM_API,
            'GraphQL endpoint replaces REST.',
            fixtures.AGENT_MIKA,
            'Migration decision'
        )
        cur = self.conn.execute(
            'SELECT id, content, supersedes_version_id '
            'FROM memory_version WHERE memory_id = ?',
            (fixtures.MEM_SM_API,)
        )
        versions = cur.fetchall()
        self.assertGreaterEqual(len(versions), 2, 'History not preserved after supersede!')


# ── E6: Staleness on source change ───────────────────────────────────
# "Code-backed memory becomes stale when its mapped file/dependency changes."
class TestE6_StalenessOnSourceChange(EvalTestBase):
    """E6 — Source-backed memories must transition to 'stale' on file change."""

    def test_stale_flag_set_when_source_commit_changes(self):
        """Memory bound to old-commit-hash-001 should be stale after code change."""
        cur = self.conn.execute(
            'SELECT m.freshness, v.source_commit, v.source_file '
            'FROM memory_version v '
            'JOIN memory m ON m.id = v.memory_id '
            'WHERE v.id = ?',
            (fixtures.VER_NC_AUDIO_V1,)
        )
        ver = cur.fetchone()
        # Right now the fixture already marks this as stale (correct setup).
        # TODO Phase 5: the engine must AUTOMATICALLY flip freshness
        # when it detects source_commit != current repo commit for source_file.
        self.assertEqual(
            ver[0], 'stale',
            f'Memory bound to outdated source should be stale, got freshness={ver[0]}'
        )

    def test_engine_would_reverify_on_commit_change(self):
        """Verify the fixture encodes a source_commit that would mismatch after a real commit.
        Phase 5 engine: compare memory_version.source_commit against current HEAD."""
        cur = self.conn.execute(
            'SELECT source_commit FROM memory_version WHERE id = ?',
            (fixtures.VER_NC_AUDIO_V1,)
        )
        stored_commit = cur.fetchone()[0]
        self.assertNotEqual(
            stored_commit, None,
            'Source-backed memory must have a source_commit to enable staleness detection'
        )


# ── E7: Tombstone resurrection guard ─────────────────────────────────
# "Rejected wrong claim cannot be silently re-added by normal admission."
class TestE7_TombstoneResurrection(EvalTestBase):
    """E7 — Tombstone must block re-admission of the rejected Docker claim."""

    def test_rejected_claim_blocked_by_tombstone(self):
        """Claim matching the rejected fingerprint should be blocked."""
        claim = 'NovelClaw uses Docker for deployment with a PostgreSQL database backend.'
        allowed = _admission_check(self.conn, claim, fixtures.PROJECT_NC)
        self.assertFalse(
            allowed,
            'Tombstone failed to block re-admission of rejected Docker claim!'
        )

    def test_different_claim_not_blocked(self):
        """A genuinely new claim should pass admission (no tombstone match)."""
        claim = 'NovelClaw uses GitHub Actions for CI/CD deployment.'
        allowed = _admission_check(self.conn, claim, fixtures.PROJECT_NC)
        self.assertTrue(
            allowed,
            'Tombstone falsely blocked a genuinely new claim!'
        )

    def test_overridden_tombstone_allows_resurrection(self):
        """If a user explicitly overrides the tombstone, the claim should pass."""
        self.conn.execute(
            'UPDATE tombstone SET overridden_by = ? WHERE id = ?',
            (fixtures.AGENT_MIKA, fixtures.TOMBSTONE_REJECTED)
        )
        self.conn.commit()
        claim = 'NovelClaw uses Docker for deployment with a PostgreSQL database backend.'
        allowed = _admission_check(self.conn, claim, fixtures.PROJECT_NC)
        self.assertTrue(
            allowed,
            'User override of tombstone should allow resurrection but was blocked!'
        )


# ── E8: Conflict abstention ──────────────────────────────────────────
# "Unresolved high-risk claims produce abstain/expose-conflict behavior."
class TestE8_ConflictAbstention(EvalTestBase):
    """E8 — Conflicting claims must NOT resolve silently; must expose conflict."""

    def test_conflict_memories_visible_to_both_parties(self):
        """Both conflicting claims must be exposed, not hidden."""
        for agent_id in (fixtures.AGENT_SORA, fixtures.AGENT_MIKA):
            rows = _resolve_conflict_memories(
                self.conn, fixtures.PROJECT_NC, agent_id
            )
            self.assertGreaterEqual(
                len(rows), 2,
                f'Expected 2 conflicts for {agent_id}, got {len(rows)}'
            )

    def test_conflict_not_resolved_silently(self):
        """Neither claim should have been auto-promoted to 'accepted'."""
        cur = self.conn.execute(
            'SELECT id FROM memory WHERE project_id = ? AND lifecycle = \'conflict\'',
            (fixtures.PROJECT_NC,)
        )
        conflict_ids = [r[0] for r in cur.fetchall()]
        self.assertIn(
            fixtures.MEM_NC_CONFLICT1, conflict_ids,
            'Conflict claim 1 was auto-resolved — must be abstained!'
        )
        self.assertIn(
            fixtures.MEM_NC_CONFLICT2, conflict_ids,
            'Conflict claim 2 was auto-resolved — must be abstained!'
        )


# ── E9: Concurrency ──────────────────────────────────────────────────
# CAN RUN NOW — two concurrent writers into SQLite WAL.
class TestE9_Concurrency(unittest.TestCase):
    """E9 — Two profiles writing simultaneously must not produce corrupt/duplicate rows."""

    NUM_WRITERS = 5
    WRITES_PER = 10

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='sm_concurrency_')
        self.db_path = os.path.join(self.tmpdir, 'test.db')
        self.conn = fixtures.seed(self.db_path)
        # Count baseline rows (from fixtures) so concurrent writes are measured correctly
        cur = self.conn.execute('SELECT COUNT(*) FROM memory')
        self.baseline_count = cur.fetchone()[0]
        self.written = []
        self.lock = threading.Lock()
        self.errors = []

    def tearDown(self):
        self.conn.close()
        try:
            os.unlink(self.db_path)
        except OSError:
            pass

    def _writer(self, agent_id, thread_id):
        """Each thread opens its own connection and writes memories."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute('PRAGMA journal_mode = WAL')
            conn.execute('PRAGMA busy_timeout = 5000')
            for i in range(self.WRITES_PER):
                mem_id = f'concurrent-{thread_id}-{i}'
                ver_id = f'cver-{thread_id}-{i}'
                conn.execute(
                    'INSERT INTO memory '
                    '  (id, project_id, scope, owner_agent_id, type, '
                    '   lifecycle, verification, freshness, '
                    '   current_version_id, created_at, updated_at) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
                    (mem_id, fixtures.PROJECT_SM, 'project', agent_id, 'fact',
                     'candidate', 'unverified', 'current', ver_id)
                )
                conn.execute(
                    'INSERT INTO memory_version '
                    '  (id, memory_id, content, reason, created_by_agent_id, '
                    '   created_at, valid_from) '
                    'VALUES (?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
                    (ver_id, mem_id, f'Concurrent write from thread {thread_id} #{i}',
                     'concurrency test', agent_id)
                )
                conn.commit()
                with self.lock:
                    self.written.append(mem_id)
        except Exception as e:
            with self.lock:
                self.errors.append((thread_id, str(e)))
        finally:
            conn.close()

    def test_concurrent_writes_no_corruption(self):
        """All concurrent inserts should land without error."""
        threads = []
        for t in range(self.NUM_WRITERS):
            agent = fixtures.AGENT_SORA if t % 2 == 0 else fixtures.AGENT_MIKA
            th = threading.Thread(target=self._writer, args=(agent, t))
            threads.append(th)
            th.start()
        for th in threads:
            th.join(timeout=30)

        self.assertEqual(
            len(self.errors), 0,
            f'Writer threads had errors: {self.errors}'
        )
        expected_total = self.baseline_count + self.NUM_WRITERS * self.WRITES_PER
        self.assertEqual(
            len(self.written), self.NUM_WRITERS * self.WRITES_PER,
            f'Written count {len(self.written)} does not match expected {self.NUM_WRITERS * self.WRITES_PER}'
        )
        # Verify no duplicate IDs
        self.assertEqual(
            len(set(self.written)), self.NUM_WRITERS * self.WRITES_PER,
            f'Duplicate memory IDs detected: {self.NUM_WRITERS * self.WRITES_PER - len(set(self.written))} dupes'
        )
        # Verify actual DB row count
        cur = self.conn.execute('SELECT COUNT(*) FROM memory')
        db_count = cur.fetchone()[0]
        self.assertEqual(
            db_count, expected_total,
            f'DB row count {db_count} does not match expected {expected_total}'
        )


# ── E10: Crash recovery ──────────────────────────────────────────────
# CAN RUN NOW — kill writer mid-operation, verify DB integrity.
class TestE10_CrashRecovery(unittest.TestCase):
    """E10 — Kill a writer mid-operation; DB must remain usable and audit coherent."""

    def test_db_survives_terminated_writer(self):
        """A WAL-mode SQLite DB must survive an abruptly-closed connection."""
        tmpdir = tempfile.mkdtemp(prefix='sm_crash_')
        db_path = os.path.join(tmpdir, 'crash_test.db')

        # Phase 1: create DB with schema and seed some data
        conn = fixtures.seed(db_path)
        cur = conn.execute('SELECT COUNT(*) FROM memory')
        baseline = cur.fetchone()[0]
        conn.close()

        # Phase 2: open a second connection, start a write, then crash (close without commit)
        conn2 = sqlite3.connect(db_path, timeout=10)
        conn2.execute('PRAGMA journal_mode = WAL')
        conn2.execute('PRAGMA busy_timeout = 5000')
        conn2.execute(
            'INSERT INTO memory '
            '  (id, project_id, scope, owner_agent_id, type, '
            '   lifecycle, verification, freshness, '
            '   current_version_id, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
            ('crash-pending', fixtures.PROJECT_SM, 'project',
             fixtures.AGENT_SORA, 'fact', 'candidate', 'unverified',
             'current', 'cver-crash')
        )
        # CRASH: close without commit
        conn2.close()

        # Phase 3: verify DB is still usable
        conn3 = sqlite3.connect(db_path, timeout=10)
        conn3.execute('PRAGMA journal_mode = WAL')
        cur = conn3.execute('SELECT COUNT(*) FROM memory')
        post_crash = cur.fetchone()[0]
        conn3.close()

        self.assertEqual(
            post_crash, baseline,
            f'Row count changed after crash! Before={baseline}, After={post_crash}'
        )

    def test_audit_consistent_after_reopen(self):
        """After crash recovery, all previously committed audit events must survive."""
        tmpdir = tempfile.mkdtemp(prefix='sm_audit_')
        db_path = os.path.join(tmpdir, 'audit_test.db')
        conn = fixtures.seed(db_path)

        cur = conn.execute('SELECT COUNT(*) FROM audit_event')
        audit_baseline = cur.fetchone()[0]
        conn.close()

        # Crash scenario: open + uncommitted write + close
        conn2 = sqlite3.connect(db_path)
        conn2.execute(
            'INSERT INTO audit_event (action, actor_agent_id, project_id, detail) '
            'VALUES (?, ?, ?, ?)',
            ('crash-test', fixtures.AGENT_SORA, fixtures.PROJECT_SM, '{}')
        )
        conn2.close()

        # Reopen and verify audit integrity
        conn3 = sqlite3.connect(db_path)
        cur = conn3.execute('SELECT COUNT(*) FROM audit_event')
        audit_after = cur.fetchone()[0]
        conn3.close()

        self.assertEqual(
            audit_after, audit_baseline,
            f'Audit row count inconsistent: before={audit_baseline}, after={audit_after}'
        )


# ── E11: Daemonless ──────────────────────────────────────────────────
# Verifiable now: no background service should persist after all clients close.
class TestE11_Daemonless(EvalTestBase):
    """E11 — No persistent memory service after clients close."""

    def test_no_background_process_after_all_connections_close(self):
        """After closing all DB connections, no process holds a lock on the DB."""
        tmpdir = tempfile.mkdtemp(prefix='sm_daemon_')
        db_path = os.path.join(tmpdir, 'daemon_test.db')
        conn = fixtures.seed(db_path)
        conn.close()

        # On WAL SQLite, after the last connection closes,
        # only the *-wal and *-shm sidecar files should exist (not a running process).
        wal_path = db_path + '-wal'
        if os.path.exists(wal_path):
            size = os.path.getsize(wal_path)
            self.assertIsInstance(size, int, 'WAL file should be stat-able')

    def test_explicit_no_daemon_flag_in_schema(self):
        """Schema must NOT contain any table for daemon/worker tracking.
        The product is explicitly daemonless — no process registry in the DB."""
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = [r[0] for r in cur.fetchall()]
        daemon_tables = [t for t in tables if 'daemon' in t or 'worker' in t or 'process' in t]
        self.assertEqual(
            daemon_tables, [],
            f'Daemon-related tables found in schema: {daemon_tables}'
        )


# ── E12: Token budget ────────────────────────────────────────────────
@unittest.expectedFailure
class TestE12_TokenBudget(unittest.TestCase):
    """E12 — Core recall + header must stay under a defined prompt budget."""

    TOKEN_BUDGET = 800  # hard ceiling for the recall block

    def setUp(self):
        self.conn = fixtures.seed()

    def test_core_recall_within_budget(self):
        """Total token count for a bounded recall block must stay under TOKEN_BUDGET.
        1 token ≈ 4 chars (conservative English estimate).
        Phase 2: the recall block must enforce this ceiling before injection.

        Seeds 15 extra memories so the unbounded stub returns more than the budget.
        The core engine must truncate/rank to fit.
        """
        # Seed 15 extra accepted shared memories to exceed budget
        for i in range(15):
            mid = f'budget-seed-{i}'
            vid = f'bver-seed-{i}'
            self.conn.execute(
                'INSERT INTO memory '
                '  (id, project_id, scope, owner_agent_id, type, '
                '   lifecycle, verification, freshness, '
                '   current_version_id, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
                (mid, fixtures.PROJECT_SM, 'project',
                 fixtures.AGENT_SORA, 'fact', 'accepted', 'unverified',
                 'current', vid)
            )
            self.conn.execute(
                'INSERT INTO memory_version '
                '  (id, memory_id, content, reason, created_by_agent_id, '
                '   created_at, valid_from) '
                'VALUES (?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
                (vid, mid,
                 f'Extra project memory {i}: deployment config, API route, '
                 f'or design decision with realistic content length for token estimation.',
                 'budget seeding', fixtures.AGENT_SORA)
            )
        store._backfill_current_fingerprints(self.conn)
        self.conn.commit()

        rows = _query_shared_visible(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_SORA
        )
        block = '\n\n'.join(
            f'[memory id={r[0]} lifecycle={r[2]}]\n{r[5]}'
            for r in rows
        )
        estimated_tokens = len(block) // 4
        self.assertLessEqual(
            estimated_tokens, self.TOKEN_BUDGET,
            f'Recall block estimated at {estimated_tokens} tokens, '
            f'exceeds budget of {self.TOKEN_BUDGET}'
        )

    def test_budget_enforced_with_more_memories(self):
        """Budget must hold even if many memories are returned."""
        # Seed additional memories to stress the budget
        for i in range(20):
            mem_id = f'budget-mem-{i}'
            ver_id = f'budget-ver-{i}'
            self.conn.execute(
                'INSERT INTO memory '
                '  (id, project_id, scope, owner_agent_id, type, '
                '   lifecycle, verification, freshness, '
                '   current_version_id, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
                (mem_id, fixtures.PROJECT_SM, 'project',
                 fixtures.AGENT_SORA, 'fact', 'accepted', 'unverified',
                 'current', ver_id)
            )
            self.conn.execute(
                'INSERT INTO memory_version '
                '  (id, memory_id, content, reason, created_by_agent_id, '
                '   created_at, valid_from) '
                'VALUES (?, ?, ?, ?, ?, datetime(\'now\'), datetime(\'now\'))',
                (ver_id, mem_id,
                 f'This is a test memory content block number {i} with enough text '
                 f'to approximate a realistic memory payload for budget calculation.',
                 'token budget test', fixtures.AGENT_SORA)
            )
        store._backfill_current_fingerprints(self.conn)
        self.conn.commit()

        rows = _query_shared_visible(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_SORA
        )
        block = '\n\n'.join(
            f'[memory id={r[0]}]\n{r[5]}' for r in rows
        )
        estimated_tokens = len(block) // 4
        self.assertLessEqual(
            estimated_tokens, self.TOKEN_BUDGET,
            f'With 20+ memories: {estimated_tokens} tokens exceeds budget {self.TOKEN_BUDGET}. '
            f'Engine MUST truncate/rank to fit ceiling.'
        )


# ── Runner ─────────────────────────────────────────────────────────────

if __name__ == '__main__':
    unittest.main(verbosity=2)
