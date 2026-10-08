"""MemCore decay/salience ranking (Adopt-1) — load-bearing contract tests.

Retention ranking refines recall order INSIDE the existing CASE ordering
family (pinned, lifecycle, verification, freshness, bm25, updated_at, id):
  retention = salience(verification, lifecycle)
              * exp(-lambda(memory_type) * age_days)
              + sigma * ln(1 + recall_count) * exp(-mu * days_since_access)

Hard constraints under test:
  - no schema change (only existing columns: updated_at, last_recalled,
    recall_count, verification, lifecycle, type)
  - decay never hides rows, never tombstones — only reorders
  - MEMCORE_ABLATE_DECAY=1 neutralizes exactly the retention term
    (byte-identical fallback to CASE+bm25 ordering)
  - MEMCORE_FAKE_NOW feeds the simulated clock (fake else real) for age
    computation only; junk falls back to the real clock, never raises

All tests use isolated tmp stores; NEVER touch live ~/.memcore/memory.db.
"""
import os
import tempfile
import unittest

from memcore import ablation, core, store

_FLAGS = ('MEMCORE_ABLATE_ALIAS_EXPANSION', 'MEMCORE_ABLATE_THAI_BIGRAM',
          'MEMCORE_ABLATE_DECAY', 'MEMCORE_FAKE_NOW')

MARKER = 'decayrank salience retention marker'


def _make_store(tmpdir, db_name='decay.db'):
    db_path = os.path.join(tmpdir, db_name)
    conn = store.open_store(db_path)
    conn.execute("INSERT INTO project (id, name) VALUES ('p-decay', 'decay')")
    conn.execute("INSERT INTO agent (id, name, profile_key) "
                 "VALUES ('a-decay', 'decay', 'decay')")
    conn.execute('INSERT INTO project_membership (project_id, agent_id, role) '
                 'VALUES (?, ?, ?)', ('p-decay', 'a-decay', 'owner'))
    conn.commit()
    return conn, db_path


class DecayRankingBase(unittest.TestCase):
    def setUp(self):
        for flag in _FLAGS:
            os.environ.pop(flag, None)
        ablation._reset_ablation_cache()
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_decayrank_')
        self.conn, self.db_path = _make_store(self.tmpdir)
        self.project = 'p-decay'
        self.agent = 'a-decay'

    def tearDown(self):
        for flag in _FLAGS:
            os.environ.pop(flag, None)
        ablation._reset_ablation_cache()
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _duplicate_pair(self, old_days_ago=60):
        """Two identical-claim rows: one aged, one fresh (same CASE keys)."""
        content = '%s alpha content' % MARKER
        old_id, _ = core.create_memory(
            self.conn, self.project, self.agent, content,
            scope='project', lifecycle='accepted')
        new_id, _ = core.create_memory(
            self.conn, self.project, self.agent, content,
            scope='project', lifecycle='accepted')
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-' || ? || ' days') "
            'WHERE id=?', (old_days_ago, old_id))
        self.conn.commit()
        return old_id, new_id

    def _order(self, limit=5):
        return [r[0] for r in core.search(
            self.conn, self.project, self.agent,
            '%s alpha content' % MARKER, limit=limit)]


class TestDecayRanking(DecayRankingBase):
    def test_old_unused_fades_below_fresh_duplicate(self):
        """Same claim, same CASE keys: the 60-day-unused row ranks below."""
        old_id, new_id = self._duplicate_pair()
        order = self._order()
        self.assertEqual(len(order), 2)
        self.assertEqual(order[0], new_id,
                         'fresh duplicate must outrank old unused row')
        self.assertEqual(order[1], old_id)

    def test_recently_recalled_resists_decay(self):
        """Reinforcement window: a recalled old row outranks the fresh dup."""
        old_id, new_id = self._duplicate_pair()
        for _ in range(3):
            self.assertEqual(core.record_recall(self.conn, [old_id]), 1)
        order = self._order()
        self.assertEqual(order[0], old_id,
                         'recently-recalled row must resist decay')
        self.assertEqual(order[1], new_id)

    def test_reinforcement_window_expires(self):
        """Stale last_recalled (beyond 14d window) loses its protection."""
        old_id, new_id = self._duplicate_pair()
        for _ in range(3):
            core.record_recall(self.conn, [old_id])
        self.assertEqual(self._order()[0], old_id)
        self.conn.execute(
            "UPDATE memory SET last_recalled=datetime('now', '-30 days') "
            'WHERE id=?', (old_id,))
        self.conn.commit()
        order = self._order()
        self.assertEqual(order[0], new_id,
                         'expired reinforcement must fade below fresh')

    def test_decay_never_hides_rows(self):
        """Decay only reorders: every eligible row is still returned."""
        old_id, new_id = self._duplicate_pair()
        rows = core.search(self.conn, self.project, self.agent,
                           '%s alpha content' % MARKER, limit=5)
        self.assertEqual({r[0] for r in rows}, {old_id, new_id})

    def test_sweep_now_override_is_deterministic(self):
        """apply_freshness_decay(now=...) override pins the clock."""
        old_id, _ = self._duplicate_pair(old_days_ago=60)
        aged, _ = core.apply_freshness_decay(
            self.conn, aging_days=30, stale_days=90,
            now='2026-10-06T00:00:00.000Z')
        self.assertIn(old_id, aged)
        # CLI path (now unset) keeps working on the remaining rows.
        self.assertIsInstance(
            core.apply_freshness_decay(self.conn), tuple)


class TestDecayAblation(DecayRankingBase):
    def test_decay_flag_flips_ordering(self):
        """DECAY=1 neutralizes the retention term: at least one flip vs unset.

        The old-but-reinforced row wins under retention and loses under the
        legacy CASE+bm25 fallback — proving the term is load-bearing.
        """
        old_id, new_id = self._duplicate_pair()
        for _ in range(3):
            core.record_recall(self.conn, [old_id])
        base = self._order()
        self.assertEqual(base[0], old_id)
        os.environ['MEMCORE_ABLATE_DECAY'] = '1'
        ablation._reset_ablation_cache()
        self.assertTrue(ablation.is_decay_ablated())
        ablated = self._order()
        self.assertEqual(ablated[0], new_id,
                         'ablated fallback must rank fresh first')
        self.assertNotEqual(base, ablated,
                            'DECAY=1 must flip at least one ordering vs unset')

    def test_decay_ablated_fallback_matches_legacy_order(self):
        """Without retention the tie-break is updated_at DESC, id ASC."""
        old_id, new_id = self._duplicate_pair()
        os.environ['MEMCORE_ABLATE_DECAY'] = '1'
        ablation._reset_ablation_cache()
        self.assertEqual(self._order(), [new_id, old_id])

    def test_fake_now_deterministic(self):
        """Same FAKE_NOW twice -> identical order; it only feeds age."""
        old_id, new_id = self._duplicate_pair()
        os.environ['MEMCORE_FAKE_NOW'] = '2026-10-06T00:00:00.000Z'
        ablation._reset_ablation_cache()
        self.assertIsNotNone(ablation.fake_now())
        first = self._order()
        ablation._reset_ablation_cache()
        second = self._order()
        self.assertEqual(first, second,
                         'same FAKE_NOW must produce the same order')
        # Far-past fake clamps all ages to 0 -> fresh-first tie-break holds.
        os.environ['MEMCORE_FAKE_NOW'] = '2020-01-01T00:00:00.000Z'
        ablation._reset_ablation_cache()
        past = self._order()
        self.assertEqual(past, [new_id, old_id])

    def test_junk_fake_now_never_crashes(self):
        """Junk FAKE_NOW falls back to the real clock and never raises."""
        self._duplicate_pair()
        for junk in ('not-a-date', '2026-13-99T99:99:99.000Z',
                     '2026-10-06 12:00:00', '', '1696500000'):
            os.environ['MEMCORE_FAKE_NOW'] = junk
            ablation._reset_ablation_cache()
            self.assertIsNone(ablation.fake_now(),
                              'junk %r must parse to None' % (junk,))
            rows = core.search(self.conn, self.project, self.agent,
                               '%s alpha content' % MARKER, limit=5)
            self.assertIsInstance(rows, list)
            self.assertEqual(len(rows), 2)



class FreshnessLabelProjectionTests(unittest.TestCase):
    """The freshness label shown in a recall line must not lie.

    ``apply_freshness_decay`` is a manual sweep (no cron by owner decision), so
    every stored label can sit at 'current' while the row is weeks old and the
    recall line would claim 'current' for something decay would have aged. The
    label is therefore projected at read time, inside the same SQL pass — never
    written, and a real sweep's stored value still wins.
    """

    def setUp(self):
        from memcore import ablation as _ablation
        _ablation._reset_ablation_cache()
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_freshlabel_')
        self.db_path = os.path.join(self.tmpdir, 'fresh.db')
        self.conn = store.open_store(self.db_path)
        self.conn.execute("INSERT INTO project (id, name) VALUES ('p-f', 'f')")
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) "
            "VALUES ('agent-f', 'f', 'f')")
        self.conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) "
            "VALUES ('p-f', 'agent-f', 'owner')")
        self.conn.commit()

    def tearDown(self):
        from memcore import ablation as _ablation
        _ablation._reset_ablation_cache()
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _aged(self, content, days):
        mem_id, _ = core.create_memory(
            self.conn, 'p-f', 'agent-f', content,
            scope='project', lifecycle='accepted')
        self.conn.execute(
            "UPDATE memory SET updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now',?) "
            'WHERE id=?', (f'-{days} days', mem_id))
        self.conn.commit()
        return mem_id

    def _label(self, mem_id, content):
        rows = core.search(self.conn, 'p-f', 'agent-f', content, limit=5)
        for row in rows:
            if row[0] == mem_id:
                return row[4]
        return None

    def test_fresh_row_reads_current(self):
        mem_id = self._aged('freshness label probe current', 2)
        self.assertEqual(self._label(mem_id, 'freshness label probe current'),
                         'current')

    def test_thirty_day_row_reads_aging(self):
        mem_id = self._aged('freshness label probe aging', 45)
        self.assertEqual(self._label(mem_id, 'freshness label probe aging'),
                         'aging')

    def test_ninety_day_row_reads_aging_like_the_sweep_writes(self):
        """The projection must never overstate decay.

        It advances at most ONE step, exactly as the sequential sweep in
        apply_freshness_decay does: 'current' -> 'aging'. Reading a 120-day row as
        'stale' was wrong — the sweep writes 'aging' for it, and only a row
        already stored 'aging' can reach 'stale'.
        """
        mem_id = self._aged('freshness label probe stale', 120)
        self.assertEqual(self._label(mem_id, 'freshness label probe stale'),
                         'aging')

    def test_stored_aging_row_reads_stale(self):
        """The second step needs the row to already be stored 'aging'."""
        mem_id = self._aged('freshness label probe two steps', 120)
        self.conn.execute(
            "UPDATE memory SET freshness='aging' WHERE id=?", (mem_id,))
        self.conn.commit()
        self.assertEqual(
            self._label(mem_id, 'freshness label probe two steps'), 'stale')

    def test_recent_recall_rescues_an_old_row(self):
        """Same reinforcement rule the durable sweep uses: a fact the fleet
        actually still recalls stays current."""
        mem_id = self._aged('freshness label probe reinforced', 60)
        core.record_recall(self.conn, [mem_id])
        self.assertEqual(
            self._label(mem_id, 'freshness label probe reinforced'), 'current')

    def test_stored_label_wins_over_projection(self):
        """A real sweep's durable value must not be second-guessed."""
        mem_id = self._aged('freshness label probe stored wins', 60)
        self.conn.execute(
            "UPDATE memory SET freshness='stale' WHERE id=?", (mem_id,))
        self.conn.commit()
        self.assertEqual(
            self._label(mem_id, 'freshness label probe stored wins'), 'stale')

    def test_projection_never_writes(self):
        self._aged('freshness label probe no write', 60)
        before = dict(self.conn.execute(
            'SELECT freshness, COUNT(*) FROM memory GROUP BY 1'))
        core.search(
            self.conn, 'p-f', 'agent-f', 'freshness label probe', limit=10)
        after = dict(self.conn.execute(
            'SELECT freshness, COUNT(*) FROM memory GROUP BY 1'))
        self.assertEqual(before, after)
        self.assertEqual(list(before), ['current'])

    def test_fake_now_drives_the_projection(self):
        mem_id = self._aged('freshness label probe fake clock', 5)
        os.environ['MEMCORE_FAKE_NOW'] = '2030-01-01T00:00:00.000Z'
        from memcore import ablation as _ablation
        _ablation._reset_ablation_cache()
        try:
            self.assertEqual(
                self._label(mem_id, 'freshness label probe fake clock'), 'aging')
        finally:
            os.environ.pop('MEMCORE_FAKE_NOW', None)
            _ablation._reset_ablation_cache()



if __name__ == '__main__':
    unittest.main()
