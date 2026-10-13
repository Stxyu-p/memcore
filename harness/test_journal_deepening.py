"""Regression tests for the deepened Journal engine (ingest.record and ingest.sweep)."""
import os
import tempfile
import unittest

from memcore import ingest, store


class TestJournalDeepening(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_jnl_deep_')
        self.db_path = os.path.join(self.tmpdir, 'journal.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-demo'
        self.alice = 'agent-alice'
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, 'demo')", (self.project,))
        self.conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (self.alice, 'alice', 'alice')
        )
        self.conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) VALUES (?, ?, ?)',
            (self.project, self.alice, 'owner')
        )

    def tearDown(self):
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def test_record_appends_and_processes_in_one_step(self):
        """ingest.record encapsulates append_event and process_event into one seam."""
        res = ingest.record(
            self.conn,
            self.project,
            self.alice,
            'turn',
            session_id='s1',
            user_content='remember that tea is preferred over coffee',
            assistant_content='Understood.'
        )
        self.assertIn('event_id', res)
        self.assertTrue(res['created'])
        self.assertEqual(res['status'], 'processed')
        self.assertIn('memory_id', res)

        # Verify event row in DB is processed, not left pending
        cur = self.conn.execute(
            'SELECT status FROM ingest_event WHERE id = ?',
            (res['event_id'],)
        )
        self.assertEqual(cur.fetchone()[0], 'processed')

    def test_record_respects_auto_process_false(self):
        """ingest.record can optionally append without immediate processing."""
        res = ingest.record(
            self.conn,
            self.project,
            self.alice,
            'turn',
            session_id='s2',
            user_content='just a chat turn',
            auto_process=False
        )
        self.assertIn('event_id', res)
        self.assertTrue(res['created'])
        self.assertEqual(res['status'], 'pending')

    def test_record_retry_processes_existing_pending_event(self):
        """A retry should recover an event durably appended before processing."""
        first = ingest.record(
            self.conn,
            self.project,
            self.alice,
            'turn',
            session_id='s3',
            user_content='remember that tea is preferred over coffee',
            auto_process=False
        )
        retried = ingest.record(
            self.conn,
            self.project,
            self.alice,
            'turn',
            session_id='s3',
            user_content='remember that tea is preferred over coffee'
        )

        self.assertEqual(retried['event_id'], first['event_id'])
        self.assertFalse(retried['created'])
        self.assertEqual(retried['status'], 'processed')
        self.assertIn('memory_id', retried)

    def test_record_retry_returns_persisted_terminal_result(self):
        """A duplicate after successful processing should report the saved result."""
        first = ingest.record(
            self.conn,
            self.project,
            self.alice,
            'turn',
            session_id='s4',
            user_content='remember that tea is preferred over coffee'
        )
        retried = ingest.record(
            self.conn,
            self.project,
            self.alice,
            'turn',
            session_id='s4',
            user_content='remember that tea is preferred over coffee'
        )

        self.assertEqual(retried['event_id'], first['event_id'])
        self.assertFalse(retried['created'])
        self.assertEqual(retried['status'], first['status'])
        self.assertEqual(retried.get('decision'), first.get('decision'))
        self.assertEqual(retried.get('memory_id'), first.get('memory_id'))

    def test_sweep_encapsulates_both_maintenance_actions(self):
        """ingest.sweep runs stale builtin dismissals and defer-cap resolutions."""
        res = ingest.sweep(self.conn, builtin_days=7, max_defers=3)
        self.assertIn('stale_builtin', res)
        self.assertIn('defer_cap', res)
        self.assertIsInstance(res['stale_builtin'], list)
        self.assertIsInstance(res['defer_cap'], list)
