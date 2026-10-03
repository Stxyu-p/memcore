"""v0.7 recall quality (Thai) + journal sweep tests.

Covers spec Part 3 (accepted outranks candidate, Golden pins surface first,
fingerprint dedup in build_recall_block) and Part 5 (stale builtin
auto-dismiss, defer-cap, sweepable vs young health).
"""
import os
import tempfile
import unittest

from memcore import core, ingest, store


class AutonomyBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_autonomy_')
        self.db_path = os.path.join(self.tmpdir, 'autonomy.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-demo'
        self.agents = ['agent-alice', 'agent-bob', 'agent-cara']
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'demo')",
            (self.project,))
        for i, aid in enumerate(self.agents):
            name = aid.removeprefix('agent-')
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, name, name))
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)',
                (self.project, aid, 'owner' if i == 0 else 'member'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class TestThaiRecallQuality(AutonomyBase):
    QUERIES = (
        ('9router gateway', '9router gateway อยู่ที่ localhost:20128'),
        ('profile switch', 'ห้ามใช้ HERMES_PROFILE ต้องใช้ hermes profile use'),
        ('Discord token', 'Discord REST ใช้ DISCORD_BOT_TOKEN กับ User-Agent กัน Cloudflare 1010'),
        ('fleet roster', 'Fleet roster: MIKA / NUA / SORA / ALTIMA / MILIM'),
        ('SOUL rules', 'SOUL v2.1 fleet rules: bad news early, brief format'),
    )

    def test_accepted_outranks_candidate_thai(self):
        cand, _ = core.create_memory(
            self.conn, self.project, self.agents[0],
            'เราตกลงกันว่าจะใช้ 9router gateway ที่ localhost:20128',
            scope='project')
        acc, _ = core.create_memory(
            self.conn, self.project, self.agents[1],
            '9router gateway หลักอยู่ที่ localhost:20128 ตั้งแต่ 2026-08-16',
            scope='project', lifecycle='accepted')
        rows = core.search(
            self.conn, self.project, self.agents[0], '9router gateway')
        ids = [r[0] for r in rows]
        self.assertIn(acc, ids)
        self.assertIn(cand, ids)
        self.assertLess(ids.index(acc), ids.index(cand))

    def test_golden_pins_surface_first(self):
        golden, _ = core.create_memory(
            self.conn, self.project, self.agents[0],
            'Fleet roster: MIKA / NUA / SORA / ALTIMA / MILIM',
            scope='project', lifecycle='accepted')
        core.set_golden(self.conn, golden, self.agents[0], True)
        plain, _ = core.create_memory(
            self.conn, self.project, self.agents[1],
            'Fleet roster สำรองสำหรับงานทั่วไป',
            scope='project', lifecycle='accepted')
        rows = core.search(
            self.conn, self.project, self.agents[0], 'Fleet roster')
        ids = [r[0] for r in rows]
        self.assertIn(golden, ids)
        self.assertIn(plain, ids)
        self.assertLess(ids.index(golden), ids.index(plain))

    def test_thai_queries_return_hits(self):
        for query, content in self.QUERIES:
            core.create_memory(
                self.conn, self.project, self.agents[0], content,
                scope='project', lifecycle='accepted')
        for query, _ in self.QUERIES:
            with self.subTest(query=query):
                rows = core.search(
                    self.conn, self.project, self.agents[0], query)
                self.assertGreater(len(rows), 0, f'no hits for {query!r}')

    def test_build_recall_block_dedups_fingerprint(self):
        import importlib.util
        import pathlib
        plugin_path = (pathlib.Path(__file__).resolve().parents[1]
                       / 'integrations' / 'hermes' / 'memcore' / 'plugin.py')
        spec = importlib.util.spec_from_file_location(
            'memcore_plugin_recall_test', plugin_path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        rows = [self.row('mem-1', 'Deploy uses go build'),
                self.row('mem-2', 'Deploy uses go build'),
                self.row('mem-3', 'Use SQLite only for local tests.')]
        block = plugin.build_recall_block([], rows, budget_chars=1200)
        # Same claim twice (different ids) collapses to one line.
        self.assertEqual(block.count('Deploy uses go build'), 1)
        self.assertIn('Use SQLite only', block)

    def test_build_recall_block_truncates_not_drops(self):
        import importlib.util
        import pathlib
        plugin_path = (pathlib.Path(__file__).resolve().parents[1]
                       / 'integrations' / 'hermes' / 'memcore' / 'plugin.py')
        spec = importlib.util.spec_from_file_location(
            'memcore_plugin_recall_test2', plugin_path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        long_row = self.row('mem-1', 'word ' * 200)
        block = plugin.build_recall_block([], [long_row], budget_chars=1200)
        self.assertIn('- [', block)
        self.assertLessEqual(len(block), 1200)
        self.assertIn('…', block)

    @staticmethod
    def row(i, content, scope='project'):
        return (i, scope, 'accepted', 'source_backed', 'current', content)


class TestJournalSweeps(AutonomyBase):
    def _builtin_unresolved(self, age_days=10):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.agents[0], 'memory_write',
            session_id=f'builtin-{age_days}-{os.urandom(2).hex()}',
            metadata={'action': 'remove', 'success': True,
                      'old_text': 'claim that matches nothing ever'})
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(
            result['decision'], 'builtin_memory_remove_unresolved_target')
        if age_days:
            self.conn.execute(
                "UPDATE ingest_event SET created_at=datetime('now', '-' || ? || ' days') "
                'WHERE id=?', (age_days, event_id))
            self.conn.commit()
        return event_id

    def test_stale_builtin_auto_dismiss_old_only(self):
        old = self._builtin_unresolved(age_days=10)
        young = self._builtin_unresolved(age_days=1)
        dismissed = ingest.auto_dismiss_stale_builtin(self.conn, days=7)
        self.assertIn(old, dismissed)
        self.assertNotIn(young, dismissed)
        status = self.conn.execute(
            'SELECT status FROM ingest_event WHERE id=?', (old,)).fetchone()[0]
        self.assertEqual(status, 'ignored')

    def test_defer_cap_dismisses_after_three_defers(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.agents[0], 'turn',
            session_id='defer-cap-1',
            user_content='ambiguous durable deployment note maybe',
            assistant_content='Acknowledged.')
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['decision'], 'semantic_review_required')
        for i in range(3):
            out = ingest.apply_semantic_analysis(
                self.conn, event_id, self.agents[0],
                analyzer=f'cap-test-{i}', verdict='defer',
                rationale='still unsure')
            self.assertIn(out['decision'], ('semantic_deferred',))
        dismissed = ingest.auto_resolve_defer_cap(self.conn, max_defers=3)
        self.assertIn(event_id, dismissed)
        status = self.conn.execute(
            'SELECT status FROM ingest_event WHERE id=?',
            (event_id,)).fetchone()[0]
        self.assertEqual(status, 'ignored')

    def test_defer_below_cap_survives(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.agents[0], 'turn',
            session_id='defer-cap-2',
            user_content='another ambiguous durable note maybe',
            assistant_content='Acknowledged.')
        ingest.process_event(self.conn, event_id)
        ingest.apply_semantic_analysis(
            self.conn, event_id, self.agents[0],
            analyzer='cap-test', verdict='defer', rationale='unsure once')
        dismissed = ingest.auto_resolve_defer_cap(self.conn, max_defers=3)
        self.assertNotIn(event_id, dismissed)

    def test_health_sweepable_vs_operator_attention(self):
        self._builtin_unresolved(age_days=10)
        snap = ingest.journal_stats(self.conn, self.project, self.agents[0])
        self.assertEqual(snap['health'], 'sweepable')
        self.assertEqual(snap['sweepable_builtin'], 1)
        self._builtin_unresolved(age_days=1)
        snap2 = ingest.journal_stats(self.conn, self.project, self.agents[0])
        self.assertEqual(snap2['health'], 'operator_attention')
        self.assertEqual(snap2['unresolved_builtin_young'], 1)


if __name__ == '__main__':
    unittest.main()
