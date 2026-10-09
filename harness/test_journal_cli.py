"""Operational journal CLI and content-redaction regression tests."""
import contextlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest

from memcore import __main__ as cli
from memcore import ingest, store


class JournalCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_journal_cli_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.project = 'proj-demo'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id,name) VALUES (?, 'demo')", (self.project,))
        for aid in (self.alice, self.bob):
            name = aid.removeprefix('agent-')
            conn.execute(
                'INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)',
                (aid, name, name)
            )
            conn.execute(
                "INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,'member')",
                (self.project, aid)
            )
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _pending(self, text='raw secret semantic payload', session='review-1'):
        conn = store.open_store(self.db)
        try:
            event_id, _ = ingest.append_event(
                conn, self.project, self.alice, 'turn', session_id=session,
                user_content=text, assistant_content='assistant raw historical reply',
                metadata={'source': 'test', 'hidden': 'metadata secret'}
            )
            result = ingest.process_event(conn, event_id)
            self.assertEqual(result['decision'], 'semantic_review_required')
            return event_id
        finally:
            conn.close()

    def test_journal_stats_is_content_free_and_counts_backlogs(self):
        semantic_event = self._pending()
        conn = store.open_store(self.db)
        try:
            ingest.apply_semantic_analysis(
                conn, semantic_event, self.alice,
                analyzer='test-analyzer', verdict='defer', confidence=0.4,
                rationale='still ambiguous'
            )
            mutation_event, _ = ingest.append_event(
                conn, self.project, self.alice, 'memory_write',
                session_id='mutation-1', metadata={
                    'action': 'remove', 'target': 'memory', 'success': True,
                    'old_text': 'raw secret unmatched mutation claim',
                }
            )
            mutation = ingest.process_event(conn, mutation_event)
            self.assertEqual(mutation['decision'], 'builtin_memory_remove_unresolved_target')
            snapshot = ingest.journal_stats(conn, self.project, self.alice)
        finally:
            conn.close()

        self.assertEqual(snapshot['total_events'], 2)
        self.assertEqual(snapshot['semantic_review_pending'], 1)
        self.assertEqual(snapshot['unresolved_builtin_mutations'], 1)
        self.assertEqual(snapshot['analysis']['total'], 1)
        self.assertEqual(snapshot['analysis']['by_verdict']['defer'], 1)
        self.assertEqual(snapshot['health'], 'operator_attention')
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn('raw secret semantic payload', rendered)
        self.assertNotIn('raw secret unmatched mutation claim', rendered)
        self.assertNotIn('metadata secret', rendered)

    def test_review_list_redacts_raw_content_until_explicitly_requested(self):
        event_id = self._pending('raw secret queue content')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'journal-review-list',
                '--project', 'demo', '--agent', 'alice'
            ])
        redacted = json.loads(output.getvalue())
        self.assertEqual(redacted['count'], 1)
        self.assertFalse(redacted['raw_content_included'])
        self.assertEqual(redacted['events'][0]['event_id'], event_id)
        self.assertNotIn('raw secret queue content', output.getvalue())
        self.assertIn('metadata_keys', redacted['events'][0])
        self.assertNotIn('metadata', redacted['events'][0])

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'journal-review-list',
                '--project', 'demo', '--agent', 'alice', '--show-content'
            ])
        revealed = json.loads(output.getvalue())
        self.assertTrue(revealed['raw_content_included'])
        self.assertIn('untrusted historical data', revealed['warning'])
        self.assertEqual(revealed['events'][0]['user_content'], 'raw secret queue content')
        self.assertEqual(revealed['events'][0]['metadata']['hidden'], 'metadata secret')

    def test_review_decide_and_analysis_history_roundtrip(self):
        event_id = self._pending('ambiguous durable deployment note')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'journal-review-decide', event_id,
                '--agent', 'alice', '--verdict', 'remember',
                '--content', 'deployment requires the migration step',
                '--confidence', '0.86', '--rationale', 'durable operational constraint'
            ])
        decision = json.loads(output.getvalue())
        self.assertEqual(decision['decision'], 'semantic_private_candidate')

        conn = store.open_store(self.db)
        try:
            memory = conn.execute(
                'SELECT scope,lifecycle,owner_agent_id FROM memory WHERE id=?',
                (decision['memory_id'],)
            ).fetchone()
            self.assertEqual(memory, ('private', 'candidate', self.alice))
        finally:
            conn.close()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'journal-analysis-history', event_id,
                '--agent', 'alice'
            ])
        history = json.loads(output.getvalue())
        self.assertEqual(history['count'], 1)
        self.assertEqual(history['analyses'][0]['verdict'], 'remember')
        self.assertEqual(history['analyses'][0]['analyzer'], 'memcore-cli')
        self.assertEqual(
            history['analyses'][0]['candidate_content'],
            'deployment requires the migration step'
        )

    def test_review_queue_is_agent_scoped(self):
        self._pending('alice-only raw review event')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'journal-review-list',
                '--project', 'demo', '--agent', 'bob', '--show-content'
            ])
        result = json.loads(output.getvalue())
        self.assertEqual(result['count'], 0)
        self.assertNotIn('alice-only raw review event', output.getvalue())

    def test_decide_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing.db')
        with self.assertRaises(SystemExit):
            cli.main([
                '--db', missing, 'journal-review-decide', 'evt-missing',
                '--agent', 'alice', '--verdict', 'ignore'
            ])
        self.assertFalse(os.path.exists(missing))

    def test_stats_and_doctor_include_journal_health(self):
        self._pending('pending operational health marker')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'stats'])
        stats = json.loads(output.getvalue())
        self.assertEqual(stats['schema_version'], '0018_memory_embedding')
        self.assertEqual(stats['journal']['semantic_review_pending'], 1)
        self.assertEqual(stats['journal']['health'], 'review_pending')

        hermes_home = os.path.join(self.tmp.name, 'isolated-hermes')
        os.makedirs(hermes_home, exist_ok=True)
        with open(os.path.join(hermes_home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write('plugins:\n  enabled: []\n')
        repo_root = pathlib.Path(cli.__file__).resolve().parents[1]
        deploy = cli._load_deploy_module(repo_root / 'scripts' / 'deploy_hermes_plugin.py')
        target = pathlib.Path(hermes_home) / 'plugins' / 'memcore'
        target.mkdir(parents=True, exist_ok=True)
        for relative in deploy.RUNTIME_FILES:
            dest = target / pathlib.PurePosixPath(relative)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(deploy.SOURCE_ROOT / pathlib.PurePosixPath(relative), dest)
        old_home = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = hermes_home
        # doctor gates on recovery readiness; this test is about journal health
        # output, so give the fixture a snapshot so that gate stays quiet.
        store.backup_store(self.db)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        finally:
            if old_home is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old_home
        self.assertIn('journal: health=review_pending', output.getvalue())

    def test_doctor_fails_on_failed_ingest_processing(self):
        event_id = self._pending('event that later represents a failed processor')
        conn = store.open_store(self.db)
        try:
            conn.execute(
                "UPDATE ingest_event SET status='failed', decision='processor_failed', "
                "error='simulated failure', processed_at=datetime('now') WHERE id=?",
                (event_id,)
            )
        finally:
            conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('journal: health=failed', output.getvalue())
        self.assertIn('failed=1', output.getvalue())


if __name__ == '__main__':
    unittest.main()
