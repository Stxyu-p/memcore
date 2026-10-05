"""Auto-accept gates table test (Task 3).

Table over the ADR-0019 lanes plus the Task 3 duplicate-merge audit:
explicit durable -> private_accepted, semantic >=0.95 -> accepted,
semantic <0.95 -> candidate, duplicate -> duplicate + duplicate-merge
audit with no new row, terminal accept -> raises, tombstone -> refuses.

Isolated store per test — NEVER touches ~/.memcore.
"""
import os
import tempfile
import unittest

from memcore import core, ingest, store


def _make_test_store():
    tmp = tempfile.mkdtemp(prefix='memcore_gates_')
    db = os.path.join(tmp, 'memory.db')
    conn = store.open_store(db)
    conn.execute("INSERT INTO project (id, name) VALUES ('proj-test','test')")
    for aid in ('agent-alice', 'agent-bob'):
        name = aid.removeprefix('agent-')
        conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (aid, name, name),
        )
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) "
        "VALUES ('proj-test','agent-alice','owner')"
    )
    conn.execute(
        "INSERT INTO project_membership (project_id, agent_id, role) "
        "VALUES ('proj-test','agent-bob','member')"
    )
    conn.commit()
    return tmp, db, conn


class AutoAcceptGatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.conn = _make_test_store()

    def tearDown(self):
        try:
            self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except Exception:
            pass
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

    def _memory_row(self, mem_id):
        return self.conn.execute(
            'SELECT scope, lifecycle, owner_agent_id FROM memory WHERE id=?',
            (mem_id,),
        ).fetchone()

    def test_explicit_durable_accepts(self):
        event_id, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-explicit',
            user_content='จำไว้ว่าฉันชอบชาอู่หลงรุ่น gate',
            assistant_content='รับทราบ',
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['decision'], 'private_accepted')
        self.assertEqual(
            self._memory_row(result['memory_id']),
            ('private', 'accepted', 'agent-alice'),
        )

    def test_semantic_high_confidence_accepts(self):
        event_id, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-sem-hi',
            user_content='gateway observation needs review tomorrow',
            assistant_content='Noted.',
        )
        pending = ingest.process_event(self.conn, event_id)
        self.assertEqual(pending['decision'], 'semantic_review_required')
        result = ingest.apply_semantic_analysis(
            self.conn, event_id, 'agent-alice',
            analyzer='gates-v1', verdict='remember',
            candidate_content='deployment requires migration step gates hi',
            confidence=0.99, rationale='Durable constraint.',
        )
        self.assertEqual(result['decision'], 'semantic_private_accepted')
        self.assertEqual(
            self._memory_row(result['memory_id'])[1], 'accepted')

    def test_semantic_low_confidence_stays_candidate(self):
        event_id, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-sem-lo',
            user_content='gateway observation needs review tomorrow',
            assistant_content='Noted.',
        )
        ingest.process_event(self.conn, event_id)
        result = ingest.apply_semantic_analysis(
            self.conn, event_id, 'agent-alice',
            analyzer='gates-v1', verdict='remember',
            candidate_content='deployment requires migration step gates lo',
            confidence=0.91, rationale='Durable constraint.',
        )
        self.assertEqual(result['decision'], 'semantic_private_candidate')
        self.assertEqual(
            self._memory_row(result['memory_id'])[1], 'candidate')

    def test_explicit_duplicate_merges_with_audit(self):
        first, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-dup-1',
            user_content='จำไว้ว่า duplicate merge probe alpha',
            assistant_content='รับทราบ',
        )
        r1 = ingest.process_event(self.conn, first)
        count_before = self.conn.execute(
            'SELECT COUNT(*) FROM memory').fetchone()[0]
        second, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-dup-2',
            user_content='จำไว้ว่า duplicate merge probe alpha',
            assistant_content='รับทราบ',
        )
        r2 = ingest.process_event(self.conn, second)
        self.assertEqual(r2['decision'], 'duplicate')
        self.assertEqual(r2['memory_id'], r1['memory_id'])
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0],
            count_before,
        )
        audit = self.conn.execute(
            "SELECT action FROM audit_event WHERE action='duplicate-merge'"
        ).fetchall()
        self.assertEqual(len(audit), 1)

    def test_semantic_duplicate_merges_with_audit(self):
        first, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-sem-dup-1',
            user_content='first ambiguous observation for dup probe',
            assistant_content='Noted.',
        )
        ingest.process_event(self.conn, first)
        created = ingest.apply_semantic_analysis(
            self.conn, first, 'agent-alice',
            analyzer='gates-v1', verdict='remember',
            candidate_content='unique semantic duplicate merge marker',
        )
        count_before = self.conn.execute(
            'SELECT COUNT(*) FROM memory').fetchone()[0]
        second, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id='gates-sem-dup-2',
            user_content='second ambiguous observation for dup probe',
            assistant_content='Noted.',
        )
        ingest.process_event(self.conn, second)
        dup = ingest.apply_semantic_analysis(
            self.conn, second, 'agent-alice',
            analyzer='gates-v1', verdict='remember',
            candidate_content='unique semantic duplicate merge marker',
        )
        self.assertEqual(dup['decision'], 'semantic_duplicate')
        self.assertEqual(dup['memory_id'], created['memory_id'])
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0],
            count_before,
        )
        audit = self.conn.execute(
            "SELECT action FROM audit_event WHERE action='duplicate-merge'"
        ).fetchall()
        self.assertEqual(len(audit), 1)

    def test_terminal_lifecycle_accept_raises(self):
        mem_id, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice',
            'terminal lifecycle probe claim gates',
            scope='project', lifecycle='candidate',
        )
        self.assertTrue(core.reject(
            self.conn, mem_id, 'agent-alice', 'terminal probe'))
        audit_before = self.conn.execute(
            'SELECT COUNT(*) FROM audit_event').fetchone()[0]
        with self.assertRaises(core.MemCoreError):
            core.accept_memory(self.conn, mem_id, 'agent-alice', 'should fail')
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)).fetchone()
        self.assertEqual(row[0], 'rejected')
        audit_after = self.conn.execute(
            'SELECT COUNT(*) FROM audit_event').fetchone()[0]
        self.assertEqual(audit_after, audit_before)

    def test_tombstone_blocked_accept_refuses(self):
        claim = 'tombstone blocked probe claim gates'
        mem_candidate, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice', claim,
            scope='project', lifecycle='candidate',
        )
        mem_other, _ = core.create_memory(
            self.conn, 'proj-test', 'agent-alice', claim,
            scope='project', lifecycle='candidate',
        )
        self.assertTrue(core.reject(
            self.conn, mem_other, 'agent-alice', 'tombstone probe'))
        self.assertIsNotNone(core._tombstone_active(
            self.conn, core.fingerprint(claim), 'proj-test'))
        with self.assertRaises(core.TombstoneBlocked):
            core.accept_memory(
                self.conn, mem_candidate, 'agent-alice', 'should veto')
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?',
            (mem_candidate,)).fetchone()
        self.assertEqual(row[0], 'candidate')


if __name__ == '__main__':
    unittest.main()
