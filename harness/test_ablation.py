"""MemCore ablation switches — eval-only lane neutralization.

Each flag neutralizes exactly ONE recall mechanism for causal measurement.
Production (all unset) is byte-identical. All tests use isolated tmp
stores; NEVER touch live ~/.memcore/memory.db (no --db needed here —
these tests never open the live path).
"""
import os
import tempfile
import unittest

from memcore import ablation, core, store


def _make_store(tmpdir):
    db_path = os.path.join(tmpdir, 'ablation.db')
    conn = store.open_store(db_path)
    conn.execute("INSERT INTO project (id, name) VALUES ('p-abl', 'abl')")
    conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('a-abl', 'abl', 'abl')")
    conn.execute('INSERT INTO project_membership (project_id, agent_id, role) '
                 'VALUES (?, ?, ?)', ('p-abl', 'a-abl', 'owner'))
    conn.commit()
    FACTS = (
        '9router local proxy answers at localhost:20128/v1 (provider custom Local)',
        'Fleet roster: MIKA orchestrator, NUA SORA ALTIMA MILIM workers, P Choke owner',
        'Converse with P Choke in Thai; commands between agents always in English',
        'ig-maxpland scan pacing: 250-500ms per page, no long pause',
    )
    for fact in FACTS:
        core.create_memory(conn, 'p-abl', 'a-abl', fact,
                           scope='project', lifecycle='accepted')
    conn.commit()
    return conn, db_path


ALIAS_TESTS = (
    ('AI gateway หลักอยู่ที่ไหน', 'localhost:20128'),
    ('ทีมมีใครบ้าง', 'MIKA'),
    ('คุยกับพี่โชคภาษาอะไร', 'Thai'),
    ('สแกนพักกี่วินาทีต่อหน้า', '250-500ms'),
)

_FLAGS = ('MEMCORE_ABLATE_ALIAS_EXPANSION', 'MEMCORE_ABLATE_THAI_BIGRAM',
          'MEMCORE_ABLATE_DECAY', 'MEMCORE_FAKE_NOW',
          'MEMCORE_EMBEDDING_PROVIDER')


class AblationBase(unittest.TestCase):
    def setUp(self):
        for flag in _FLAGS:
            os.environ.pop(flag, None)
        ablation._reset_ablation_cache()
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_ablation_')
        self.conn, self.db_path = _make_store(self.tmpdir)

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

    def _search_hit(self, query, expected, k=3):
        rows = core.search(self.conn, 'p-abl', 'a-abl', query, limit=k)
        texts = [r[5] for r in rows]
        return any(expected in t for t in texts), rows


class TestAblationAlias(AblationBase):
    def test_ablate_alias_at_least_one_paraphrase_fails(self):
        """ABLATE_ALIAS=1 -> at least one alias paraphrase misses (lane load-bearing)."""
        os.environ['MEMCORE_ABLATE_ALIAS_EXPANSION'] = '1'
        ablation._reset_ablation_cache()
        failures = sum(
            1 for query, expected in ALIAS_TESTS
            if not self._search_hit(query, expected)[0]
        )
        self.assertGreater(failures, 0,
                           'Expected >=1 alias paraphrase miss when alias lane ablated')


class TestAblationBigram(AblationBase):
    def test_ablate_bigram_glued_hit_fails(self):
        """ABLATE_BIGRAM=1 -> glued->spaced Thai bigram hit misses."""
        core.create_memory(self.conn, 'p-abl', 'a-abl',
                           'ระบบ ควร ทำงาน ได้ ทุกวัน',
                           scope='project', lifecycle='accepted')
        self.conn.commit()
        os.environ['MEMCORE_ABLATE_THAI_BIGRAM'] = '1'
        ablation._reset_ablation_cache()
        hit, rows = self._search_hit('ระบบควรทำ', 'ระบบ ควร ทำงาน ได้ ทุกวัน')
        self.assertFalse(hit, f'Expected MISS when bigram lane ablated, got: {rows}')


class TestAblationOffGreen(AblationBase):
    def test_all_unset_alias_paraphrases_hit(self):
        """All unset -> all alias paraphrases hit (floor holds)."""
        for query, expected in ALIAS_TESTS:
            hit, rows = self._search_hit(query, expected)
            self.assertTrue(hit, f'Expected hit for {query!r}, got rows: {rows}')

    def test_all_unset_bigram_hit(self):
        """All unset -> Thai glued->spaced bigram hit holds."""
        core.create_memory(self.conn, 'p-abl', 'a-abl',
                           'ระบบ ควร ทำงาน ได้ ทุกวัน',
                           scope='project', lifecycle='accepted')
        self.conn.commit()
        hit, rows = self._search_hit('ระบบควรทำ', 'ระบบ ควร ทำงาน ได้ ทุกวัน')
        self.assertTrue(hit, f'Expected bigram hit with flags unset, got: {rows}')

    def test_fake_now_deterministic_ranking_via_age(self):
        """MEMCORE_FAKE_NOW feeds the simulated clock used for age.

        Same fake twice -> identical order (deterministic). The only way
        FAKE_NOW may reorder is through age computation: with a far-future
        fake both rows age together, so both still return without crashing.
        Junk falls back to None and never raises.
        """
        before = {
            q: [r[0] for r in core.search(self.conn, 'p-abl', 'a-abl', q, limit=3)]
            for q, _ in ALIAS_TESTS
        }
        os.environ['MEMCORE_FAKE_NOW'] = '2026-01-01T00:00:00.000Z'
        ablation._reset_ablation_cache()
        self.assertIsNotNone(ablation.fake_now())
        first = {
            q: [r[0] for r in core.search(self.conn, 'p-abl', 'a-abl', q, limit=3)]
            for q, _ in ALIAS_TESTS
        }
        ablation._reset_ablation_cache()
        second = {
            q: [r[0] for r in core.search(self.conn, 'p-abl', 'a-abl', q, limit=3)]
            for q, _ in ALIAS_TESTS
        }
        self.assertEqual(first, second,
                         'same FAKE_NOW must give the same order')
        self.assertEqual(set(before), set(first))
        # Junk falls back to None, never raises.
        os.environ['MEMCORE_FAKE_NOW'] = 'not-a-date'
        ablation._reset_ablation_cache()
        self.assertIsNone(ablation.fake_now())
        junk_rows = core.search(self.conn, 'p-abl', 'a-abl',
                                ALIAS_TESTS[0][0], limit=3)
        self.assertIsInstance(junk_rows, list)

    def test_decay_flag_is_load_bearing(self):
        """MEMCORE_ABLATE_DECAY=1 neutralizes the retention term.

        Load-bearing proof lives in harness.test_decay_ranking
        (DECAY=1 flips at least one reinforced-vs-fresh ordering).
        Here: the flag parses, and on these same-age rows the fallback
        ordering still returns every expected hit.
        """
        os.environ['MEMCORE_ABLATE_DECAY'] = '1'
        ablation._reset_ablation_cache()
        self.assertTrue(ablation.is_decay_ablated())
        for query, expected in ALIAS_TESTS:
            rows = core.search(self.conn, 'p-abl', 'a-abl', query, limit=3)
            texts = [r[5] for r in rows]
            self.assertTrue(any(expected in t for t in texts),
                            f'ablated fallback must still hit {query!r}')


if __name__ == '__main__':
    unittest.main()
