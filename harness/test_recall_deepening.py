"""Regression tests for the deepened core.recall interface."""
import hashlib
import os
import pathlib
import tempfile
import unittest

import fixtures
from memcore import core


class TestRecallDeepening(unittest.TestCase):
    def setUp(self):
        self.conn = fixtures.seed()

    def tearDown(self):
        self.conn.close()

    def test_recall_returns_matching_memories_and_reinforces_access(self):
        """core.recall encapsulates search, access reinforcement, and freshness."""
        # Query memories for MIKA in memcore project
        rows = core.recall(
            self.conn,
            fixtures.PROJECT_SM,
            fixtures.AGENT_MIKA,
            'vanilla HTML/JS frontend',
            limit=5
        )
        self.assertTrue(len(rows) > 0, 'recall must return matching memories')
        top = rows[0]
        # Shape: (id, scope, lifecycle, verification, freshness, content, owner, rank, fp)
        self.assertEqual(top[0], fixtures.MEM_SM_FRONTEND)
        self.assertEqual(top[4], 'current')

        # Access reinforcement verification: recall_count must be bumped
        cur = self.conn.execute(
            'SELECT recall_count, last_recalled FROM memory WHERE id = ?',
            (fixtures.MEM_SM_FRONTEND,)
        )
        count, last_recalled = cur.fetchone()
        self.assertGreaterEqual(count, 1, 'recall must increment recall_count')
        self.assertIsNotNone(last_recalled, 'recall must set last_recalled')

    def test_finalize_recall_handles_pinned_and_search_rows_together(self):
        """Mixed provider row shapes share access tracking and freshness checks."""
        pinned, _ = core.create_memory(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_MIKA,
            'unrelated pinned lifecycle marker', scope='project'
        )
        matched, _ = core.create_memory(
            self.conn, fixtures.PROJECT_SM, fixtures.AGENT_MIKA,
            'query matched lifecycle marker', scope='project'
        )
        selected = [
            (pinned, 'project', 'accepted', 'unverified', 'current',
             'unrelated pinned lifecycle marker'),
            (matched, 'project', 'accepted', 'unverified', 'current',
             'query matched lifecycle marker', fixtures.AGENT_MIKA, 0.0,
             core.fingerprint('query matched lifecycle marker')),
        ]

        finalized = core.finalize_recall(self.conn, selected)
        self.assertEqual([row[0] for row in finalized], [pinned, matched])
        counts = dict(self.conn.execute(
            'SELECT id, recall_count FROM memory WHERE id IN (?, ?)',
            (pinned, matched)
        ).fetchall())
        self.assertEqual(counts, {pinned: 1, matched: 1})

    def test_recall_marks_a_memory_stale_when_its_source_file_changes(self):
        """Staleness follows the exact source file content, not a repository HEAD."""
        with tempfile.TemporaryDirectory() as tmp:
            source = pathlib.Path(tmp) / 'source.py'
            source.write_text('REVISION = 1\n', encoding='utf-8')
            memory_id, _ = core.create_memory(
                self.conn, fixtures.PROJECT_NC, fixtures.AGENT_MIKA,
                'Aurora checksum source is revision one.',
                scope='project', source_file=str(source),
                source_commit='old-repository-commit'
            )
            expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            saved = self.conn.execute(
                'SELECT source_hash, source_file FROM memory_version WHERE memory_id=?',
                (memory_id,)
            ).fetchone()
            self.assertEqual(saved, (expected_hash, str(source.resolve())))

            unchanged = core.recall(
                self.conn, fixtures.PROJECT_NC, fixtures.AGENT_MIKA,
                'Aurora checksum', limit=5
            )
            self.assertEqual(next(r for r in unchanged if r[0] == memory_id)[4], 'current')

            source.write_text('REVISION = 2\n', encoding='utf-8')
            changed = core.recall(
                self.conn, fixtures.PROJECT_NC, fixtures.AGENT_MIKA,
                'Aurora checksum', limit=5
            )
            self.assertEqual(next(r for r in changed if r[0] == memory_id)[4], 'stale')

    def test_recall_encapsulates_source_file_staleness_flip(self):
        """A changed tracked file is detected by the unified recall path."""
        # MEM_NC_AUDIO has a baseline hash that differs from pyproject.toml.
        # Reset freshness to 'current' to verify the live file-hash flip inside recall.
        self.conn.execute(
            "UPDATE memory SET freshness='current' WHERE id = ?",
            (fixtures.MEM_NC_AUDIO,)
        )
        self.conn.commit()

        # The fixture's saved source hash differs from the current pyproject.toml.
        rows = core.recall(
            self.conn,
            fixtures.PROJECT_NC,
            fixtures.AGENT_MIKA,
            'Edge-TTS Chinese voice synthesis',
            limit=5,
            get_head=lambda: 'new-commit-hash-002'
        )
        audio_hits = [r for r in rows if r[0] == fixtures.MEM_NC_AUDIO]
        self.assertEqual(len(audio_hits), 1)
        # The returned freshness must immediately reflect 'stale'
        self.assertEqual(audio_hits[0][4], 'stale')

        # In DB, the row must be flipped to stale without touching updated_at
        cur = self.conn.execute(
            'SELECT freshness FROM memory WHERE id = ?',
            (fixtures.MEM_NC_AUDIO,)
        )
        self.assertEqual(cur.fetchone()[0], 'stale')

    def test_recall_is_safe_on_readonly_connection(self):
        """core.recall returns results safely even when connection is strictly read-only."""
        import tempfile, sqlite3
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        self.addCleanup(lambda: os.unlink(tmp.name))

        # Seed database to the temp file
        conn = fixtures.seed(db_path=tmp.name)
        conn.close()

        # Open in URI read-only mode
        ro_conn = sqlite3.connect(f'file:{tmp.name}?mode=ro', uri=True)
        try:
            rows = core.recall(
                ro_conn,
                fixtures.PROJECT_SM,
                fixtures.AGENT_MIKA,
                'vanilla HTML/JS',
                limit=5
            )
            self.assertTrue(len(rows) > 0, 'read-only recall must succeed')
            self.assertEqual(rows[0][0], fixtures.MEM_SM_FRONTEND)
        finally:
            ro_conn.close()

    def test_recall_does_not_depend_on_git_repository(self):
        """Recall verifies tracked file contents without asking Git for HEAD."""
        rows = core.recall(
            self.conn,
            fixtures.PROJECT_SM,
            fixtures.AGENT_MIKA,
            'vanilla HTML/JS',
            limit=5,
        )
        self.assertTrue(len(rows) > 0, 'recall must not depend on Git availability')
