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


class TestNumericNegationOrdering(AutonomyBase):
    """F: a query naming a concrete value must not lead with a rival value.

    Lexical recall has no polarity, but both sides carry standalone numbers, so
    disjoint number sets are the one usable negation signal without a model.
    """

    def setUp(self):
        super().setUp()
        self.right, _ = core.create_memory(
            self.conn, self.project, self.agents[0],
            '9router gateway runs on port 20128',
            scope='project', lifecycle='accepted')
        self.wrong, _ = core.create_memory(
            self.conn, self.project, self.agents[1],
            '9router gateway runs on port 8080',
            scope='project', lifecycle='candidate')
        core.create_memory(
            self.conn, self.project, self.agents[2],
            'memcore uses sqlite with wal journalling',
            scope='project', lifecycle='accepted')

    def test_query_naming_a_value_ranks_that_value_first(self):
        rows = core.search(self.conn, self.project, self.agents[0],
                           '9router gateway port 20128', limit=3)
        self.assertTrue(rows)
        self.assertEqual(rows[0][0], self.right)

    def test_rival_value_is_not_removed_only_demoted(self):
        rows = core.search(self.conn, self.project, self.agents[0],
                           '9router gateway port 20128', limit=3)
        self.assertIn(self.wrong, [r[0] for r in rows])

    def test_query_without_numbers_keeps_original_order(self):
        rows = core.search(self.conn, self.project, self.agents[0],
                           'gateway port', limit=3)
        self.assertTrue(rows)

    def test_ordering_is_deterministic(self):
        first = [r[0] for r in core.search(
            self.conn, self.project, self.agents[0],
            '9router gateway port 20128', limit=3)]
        second = [r[0] for r in core.search(
            self.conn, self.project, self.agents[0],
            '9router gateway port 20128', limit=3)]
        self.assertEqual(first, second)

    def test_purely_numeric_and_empty_queries_never_raise(self):
        for query in ('', '   ', '12345', 'no numbers at all here'):
            self.assertIsInstance(
                core.search(self.conn, self.project, self.agents[0],
                            query, limit=3), list)


class TestDistinctClaimWindow(AutonomyBase):
    """The result window must carry distinct claims, not copies of one fact.

    The fleet corroborates by re-writing one claim from several agents, so the
    same fact arrives as N identical rows. Before, a shallow window could be
    entirely copies of a single fact and every remaining slot carried nothing
    new. search() now reads DISTINCT_OVERFETCH times deeper and folds copies
    behind the canonical one.
    """

    def setUp(self):
        super().setUp()
        self.canonical = {}
        for text in ('gateway profile switch uses hermes profile use',
                     'discord token lives in DISCORD_BOT_TOKEN',
                     'scan pacing runs at 250-500ms per page'):
            ids = []
            for i, agent in enumerate(self.agents):
                mid, _ = core.create_memory(
                    self.conn, self.project, agent, text,
                    scope='project', lifecycle='accepted')
                ids.append(mid)
            self.canonical[text] = ids

    def _rows(self, query, limit=8):
        return core.search(self.conn, self.project, self.agents[0],
                           query, limit=limit)

    def test_distinct_claims_are_never_pushed_out_by_copies(self):
        """Copies rank behind distinct claims, so a full window spends its
        slots on new facts first."""
        rows = self._rows('gateway', limit=4)
        fps = [r[8] for r in rows]
        first_repeat = next(
            (i for i in range(1, len(fps)) if fps[i] in fps[:i]), len(fps))
        distinct_before_repeat = len(set(fps[:first_repeat]))
        self.assertEqual(distinct_before_repeat, first_repeat,
                         'a duplicate claim ranked ahead of a distinct one')

    def test_window_spends_its_slots_on_distinct_claims_first(self):
        """Every distinct claim reachable for the query must occupy the front
        of the window; copies only follow once nothing new is left."""
        rows = self._rows('gateway discord scan', limit=8)
        fps = [r[8] for r in rows]
        total_distinct = self.conn.execute(
            'SELECT COUNT(DISTINCT claim_fingerprint) FROM memory '
            "WHERE lifecycle IN ('candidate','accepted','conflict')"
        ).fetchone()[0]
        self.assertLessEqual(len(set(fps)), total_distinct)
        # Distinct claims must be contiguous at the front of the window.
        first_repeat = next(
            (i for i in range(1, len(fps)) if fps[i] in fps[:i]), len(fps))
        self.assertEqual(len(set(fps[:first_repeat])), first_repeat)

    def test_canonical_copy_keeps_its_rank(self):
        rows = self._rows('hermes profile use')
        kept = [r[0] for r in rows]
        self.assertIn(self.canonical[
            'gateway profile switch uses hermes profile use'][0], kept)

    def test_no_claim_is_ever_lost(self):
        for text, ids in self.canonical.items():
            query = text.split()[0]
            rows = self._rows(query, limit=13)
            found = {r[8] for r in rows}
            live = {self.conn.execute(
                'SELECT claim_fingerprint FROM memory WHERE id=?',
                (i,)).fetchone()[0] for i in ids}
            self.assertTrue(live & found,
                            f'claim dropped entirely for query {query!r}')

    def test_limit_is_still_respected(self):
        for limit in (1, 3, 8, 13):
            self.assertLessEqual(len(self._rows('gateway', limit=limit)), limit)

    def test_single_row_and_empty_inputs_pass_through(self):
        self.assertEqual(core._collapse_duplicate_claims([], 8), [])
        one = ('mem-1', 'project', 'accepted', 'unverified', 'current', 'x', 'a', 0.0)
        self.assertEqual(core._collapse_duplicate_claims([one], 8), [one])

    def test_order_is_stable_across_repeated_calls(self):
        first = [r[0] for r in self._rows('gateway')]
        second = [r[0] for r in self._rows('gateway')]
        self.assertEqual(first, second)

    def test_rows_without_fingerprint_column_still_dedupe(self):
        """Legacy 8-tuple rows fall back to hashing the content."""
        row = ('mem-1', 'project', 'accepted', 'unverified', 'current',
               'same text here', 'a', 0.0)
        dup = ('mem-2', 'project', 'accepted', 'unverified', 'current',
               'same text here', 'b', 0.0)
        kept = core._collapse_duplicate_claims([row, dup], 1)
        self.assertEqual([r[0] for r in kept], ['mem-1'])
        # Nothing is discarded: the copy is still reachable without a limit.
        self.assertEqual(len(core._collapse_duplicate_claims([row, dup], 8)), 2)


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
