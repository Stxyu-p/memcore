"""Observability tests (Task 4): doctor journal-age + stats autonomy counts.

Content-free checks only — no memory text may appear in doctor/stats output.
Isolated store per test — NEVER touches ~/.memcore.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from memcore import core, ingest, store
from memcore import __main__ as cli


def _utc_z(days_ago=0):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime(
        '%Y-%m-%dT%H:%M:%SZ')


def _make_store():
    tmp = tempfile.mkdtemp(prefix='memcore_obs_')
    db = os.path.join(tmp, 'memory.db')
    conn = store.open_store(db)
    conn.execute("INSERT INTO project (id, name) VALUES ('proj-test','test')")
    for aid, role in (('agent-alice', 'owner'), ('agent-bob', 'member')):
        name = aid.removeprefix('agent-')
        conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (aid, name, name),
        )
        conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)',
            ('proj-test', aid, role),
        )
    conn.commit()
    return tmp, db, conn


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.conn = _make_store()

    def tearDown(self):
        if getattr(self, 'conn', None) is not None:
            try:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _close_conn(self):
        try:
            self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except Exception:
            pass
        self.conn.close()
        self.conn = None

    def _seed_pending(self, session, content, days_ago=None):
        event_id, _ = ingest.append_event(
            self.conn, 'proj-test', 'agent-alice', 'turn',
            session_id=session, user_content=content,
            assistant_content='Noted.',
        )
        if days_ago is not None:
            self.conn.execute(
                'UPDATE ingest_event SET created_at=? WHERE id=?',
                (_utc_z(days_ago), event_id),
            )
            self.conn.commit()
        return event_id

    def test_doctor_journal_age(self):
        self._seed_pending('obs-old', 'oldest pending probe alpha', days_ago=9)
        self._seed_pending('obs-fresh', 'fresh pending probe beta')
        age = ingest.journal_age(self.conn)
        self.assertEqual(age['oldest_pending_days'], 9)
        self.conn.execute('DELETE FROM ingest_event')
        self.conn.commit()
        self.assertIsNone(
            ingest.journal_age(self.conn)['oldest_pending_days'])

    def test_doctor_prints_journal_age(self):
        self._seed_pending('obs-doc', 'doctor print probe gamma', days_ago=9)
        self._close_conn()
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        except SystemExit:
            pass
        text = output.getvalue()
        self.assertIn('journal age:', text)
        self.assertIn('9d', text)

    def test_stats_autonomy_counts(self):
        canary = 'canary content zebra quasar obs'
        core.create_memory(
            self.conn, 'proj-test', 'agent-alice', canary,
            scope='project', lifecycle='candidate',
        )
        day1, day2 = '2026-09-20T10:00:00Z', '2026-09-21T10:00:00Z'
        for action, actor, created in (
            ('auto_corrob_accept', 'agent-alice', day1),
            ('duplicate-merge', 'agent-alice', day1),
            ('contradiction-hold', 'agent-bob', day2),
        ):
            self.conn.execute(
                'INSERT INTO audit_event '
                '(action, actor_agent_id, project_id, detail, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (action, actor, 'proj-test', '{}', created),
            )
        self.conn.commit()
        self._close_conn()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'stats'])
        text = output.getvalue()
        data = json.loads(text)
        per_day = data['autonomy_per_day']
        self.assertEqual(per_day['2026-09-20']['auto_corrob_accept'], 1)
        self.assertEqual(per_day['2026-09-20']['duplicate-merge'], 1)
        self.assertEqual(per_day['2026-09-21']['contradiction-hold'], 1)
        self.assertNotIn('zebra quasar', text)


if __name__ == '__main__':
    unittest.main()
