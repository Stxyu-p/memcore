"""Contradiction pre-accept gate tests (Task 1 + Task 2 wiring).

Tests the read-only helper and its integration into all auto-accept entries.
Isolated store per test_dispatch_roundtrip.py pattern — NEVER touches ~/.memcore.
"""
import os
import tempfile
import unittest

from memcore import core, store
from memcore import contradiction as cd


def _make_test_store():
    tmp = tempfile.mkdtemp(prefix='memcore_gate_')
    db = os.path.join(tmp, 'memory.db')
    conn = store.open_store(db)
    conn.execute("INSERT INTO project (id, name) VALUES ('proj-test','test')")
    conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-alice','alice','alice')")
    conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-bob','bob','bob')")
    conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-carol','carol','carol')")
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-test','agent-alice','owner')"
    )
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-test','agent-bob','member')"
    )
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-test','agent-carol','member')"
    )
    conn.commit()
    return tmp, db, conn


class ContradictionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.conn = _make_test_store()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    # ---------- Task 1: pre_accept_conflict_check read-only helper ----------

    def test_precheck_polarity_hold(self):
        """Seed 'ใช้ X' (affirming) + candidate 'ห้ามใช้ X' (negating) → hold, reason 'polarity'."""
        # Create an accepted memory with Thai affirmative
        mem_id, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'ใช้ gateway 9router สำหรับการเชื่อมต่อ AI',
            scope='project', lifecycle='accepted',
        )
        # Pre-check with contradictory negative
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', 'ห้ามใช้ gateway 9router สำหรับการเชื่อมต่อ AI',
            exclude_memory_id=None,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], mem_id)
        self.assertEqual(hits[0][1], 'polarity')

    def test_precheck_numeric_hold(self):
        """Seed 'port 20128' + candidate 'port 8080' on same subject → hold, reason 'numeric'."""
        mem_id, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            '9router listens on port 20128 for incoming requests',
            scope='project', lifecycle='accepted',
        )
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', '9router listens on port 8080 for incoming requests',
            exclude_memory_id=None,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], mem_id)
        self.assertEqual(hits[0][1], 'numeric')

    def test_number_boundary_identifiers_ignored(self):
        """ALTIMA minor: digits glued inside identifiers are not claims.

        '9router' contributes no '9'; 'v1' contributes no '1'. Standalone
        ports ('port 20128') still count. stdlib-only, no segmenter.
        """
        self.assertEqual(cd.numbers('9router listens on port 20128 v1'), frozenset({'20128'}))
        self.assertEqual(cd.numbers('9router v1'), frozenset())
        self.assertEqual(cd.numbers('port 8080 vs 20128'), frozenset({'8080', '20128'}))
        # Same identifier on both sides with no standalone numbers: not numeric.
        hit, reason = cd.is_contradiction_pair(
            'use 9router gateway', 'use 9router gateway v1')
        self.assertFalse(hit)
        self.assertNotEqual(reason, 'numeric')

    def test_precheck_clean(self):
        """Unrelated subjects → empty list (clean)."""
        core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'The sky is blue on a clear day',
            scope='project', lifecycle='accepted',
        )
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', 'The ocean is vast and deep',
            exclude_memory_id=None,
        )
        self.assertEqual(hits, [])

    def test_precheck_empty_key_holds(self):
        """Content whose subject_key is empty → non-empty hold (fails closed)."""
        # Pure numbers/particles - subject_key returns ''
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', '20128 8080 443',
            exclude_memory_id=None,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], '__empty_subject__')
        self.assertEqual(hits[0][1], 'empty_subject_hold')

    def test_precheck_excludes_given_id(self):
        """exclude_memory_id excludes that row from the check."""
        mem_id, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'ใช้ gateway 9router',
            scope='project', lifecycle='accepted',
        )
        # Exclude the same memory - should be clean
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', 'ห้ามใช้ gateway 9router',
            exclude_memory_id=mem_id,
        )
        self.assertEqual(hits, [])

    def test_precheck_only_live_rows(self):
        """Only candidate/accepted/conflict rows are checked; rejected/disabled/superseded excluded."""
        mem_accepted, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'ใช้ gateway 9router',
            scope='project', lifecycle='accepted',
        )
        mem_rejected, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-bob',
            'ใช้ gateway 9router',
            scope='project', lifecycle='candidate',
        )
        mem_disabled, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-carol',
            'ใช้ gateway 9router',
            scope='project', lifecycle='candidate',
        )
        # Terminal lifecycles are engine transitions; set directly so the
        # precheck filter (not the transition logic) is what this tests.
        self.conn.execute(
            "UPDATE memory SET lifecycle='rejected' WHERE id=?", (mem_rejected,))
        self.conn.execute(
            "UPDATE memory SET lifecycle='disabled' WHERE id=?", (mem_disabled,))
        self.conn.commit()
        # Should only see the accepted one
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', 'ห้ามใช้ gateway 9router',
            exclude_memory_id=None,
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], mem_accepted)

    # ---------- Task 2: Gate wired into all auto-accept entries ----------

    def test_explicit_lane_blocked(self):
        """Explicit durable ingest with contradiction → no accepted, audit contradiction-hold."""
        from memcore import ingest

        # Seed a live accepted memory
        core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'ใช้ gateway 9router สำหรับ AI',
            scope='project', lifecycle='accepted',
        )

        # Ingest an explicit durable signal contradicting the seed.
        # Engine path: append_event + process_event (no ingest_event API).
        event_id, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-bob', 'turn',
            session_id='gate-explicit',
            user_content='จำไว้ว่า ห้ามใช้ gateway 9router สำหรับ AI',
            assistant_content='รับทราบ',
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertTrue(result.get('held'))
        self.assertEqual(result.get('decision'), 'contradiction_hold')
        # Per plan Task 2: no accepted row — both rows conflict when the
        # actor may mark (bob is member, seed is project scope).
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?',
            (result['memory_id'],),
        ).fetchone()
        self.assertEqual(row[0], 'conflict')
        # Check audit for contradiction-hold
        audit = self.conn.execute(
            "SELECT action FROM audit_event WHERE action='contradiction-hold'"
        ).fetchall()
        self.assertEqual(len(audit), 1)

    def test_semantic_lane_blocked(self):
        """Semantic high-confidence (>=0.95) with contradiction → blocked, audit contradiction-hold."""
        from memcore import ingest

        core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            '9router gateway runs on port 20128',
            scope='project', lifecycle='accepted',
        )

        # Ambiguous turn stays pending for semantic review (engine path).
        event_id, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-bob', 'turn',
            session_id='gate-semantic',
            user_content='gateway observation needs review tomorrow',
            assistant_content='Noted.',
        )
        pending = ingest.process_event(self.conn, event_id)
        self.assertEqual(pending['decision'], 'semantic_review_required')

        # High-confidence remember verdict contradicting the seed (numeric).
        result = ingest.apply_semantic_analysis(
            self.conn, event_id, 'agent-bob',
            analyzer='gate-analyzer', verdict='remember',
            candidate_content='9router gateway runs on port 8080',
            confidence=0.99, rationale='Durable operational constraint.',
        )
        self.assertTrue(result.get('held'))
        self.assertEqual(result.get('decision'), 'contradiction_hold')
        # The new row must NOT have been accepted — it stays candidate.
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?',
            (result['memory_id'],),
        ).fetchone()
        self.assertEqual(row[0], 'candidate')
        audit = self.conn.execute(
            "SELECT action FROM audit_event WHERE action='contradiction-hold'"
        ).fetchall()
        self.assertEqual(len(audit), 1)

    def test_corrob_blocked(self):
        """Third writer completing corroboration while contradiction exists → maybe_auto_corrob returns contradiction_hold."""
        from memcore import core

        # Seed two writers with same claim
        core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'ใช้ gateway 9router',
            scope='project', lifecycle='accepted',
        )
        core.create_memory(
            self.conn, 'proj-test', 'agent-bob',
            'ใช้ gateway 9router',
            scope='project', lifecycle='accepted',
        )
        # Add a contradictory accepted memory
        core.create_memory(
            self.conn, 'proj-test', 'agent-carol',
            'ห้ามใช้ gateway 9router',
            scope='project', lifecycle='accepted',
        )

        # Now third writer of the original claim tries to trigger corroboration
        claim_fp = core.fingerprint('ใช้ gateway 9router')
        result = core.maybe_auto_corrob(self.conn, 'proj-test', claim_fp, 'agent-carol')
        self.assertEqual(result.get('action'), 'contradiction_hold')
        self.assertNotEqual(result.get('action'), 'accepted')
        # Check audit
        audit = self.conn.execute(
            "SELECT action FROM audit_event WHERE action='contradiction-hold'"
        ).fetchall()
        self.assertEqual(len(audit), 1)

    def test_tombstone_between_precheck_and_commit(self):
        """Pre-check clean, then tombstone created before commit → commit refuses, veto wins."""
        from memcore import core, store

        # Create a clean accepted memory
        mem_id, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'MemCore uses SQLite with WAL',
            scope='project', lifecycle='accepted',
        )
        claim_fp = core.fingerprint('MemCore uses SQLite with WAL')

        # Pre-check is clean — sanity: self excluded, no live rival.
        hits = core.pre_accept_conflict_check(
            self.conn, 'proj-test', 'MemCore uses SQLite with WAL',
            exclude_memory_id=mem_id,
        )
        self.assertEqual(hits, [])

        # Simulate the race: a governing reject lands the tombstone for that
        # claim between the pre-check and the later commit. Use the engine
        # path (reject) so both the terminal lifecycle and the refusal guard
        # exist, exactly like a real interleaving would leave them.
        self.assertTrue(core.reject(
            self.conn, mem_id, 'agent-alice', 'race veto'))
        self.assertIsNotNone(core._tombstone_active(
            self.conn, claim_fp, 'proj-test'))

        # A later create with the same claim must be vetoed at write time —
        # TombstoneBlocked from create_memory itself, not a silent row.
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, 'proj-test', 'agent-bob',
                'MemCore uses SQLite with WAL',
                scope='project', lifecycle='candidate',
            )


if __name__ == '__main__':
    unittest.main()