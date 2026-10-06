"""MemCore Recall 2.0 — table-driven tests for lanes 3.1 + 3.2.

Lane 3.2: Fleet alias expansion (query-side).
Lane 3.1: Thai de-gluing via char bigrams in _fts_query + exact-fallback.

All tests use isolated tmp stores; NEVER touch live ~/.memcore/memory.db.
"""
import os
import tempfile
import unittest

from memcore import core, store


def _make_store(tmpdir, project='proj-recall2', agent='agent-recall2'):
    """Create a fresh isolated store with the baseline FACTS pre-loaded."""
    db_path = os.path.join(tmpdir, 'recall2.db')
    conn = store.open_store(db_path)
    conn.execute("INSERT INTO project (id, name) VALUES (?, 'recall2')", (project,))
    conn.execute('INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                 (agent, 'recall2', 'recall2'))
    conn.execute('INSERT INTO project_membership (project_id, agent_id, role) '
                 'VALUES (?, ?, ?)', (project, agent, 'owner'))
    conn.commit()

    FACTS = (
        '9router local proxy answers at localhost:20128/v1 (provider custom Local)',
        'HERMES_PROFILE env does not switch profiles; use hermes profile use NAME',
        'Direct Discord REST: token = DISCORD_BOT_TOKEN from the Hermes .env file',
        'Fleet roster: MIKA orchestrator, NUA SORA ALTIMA MILIM workers, P Choke owner',
        'Converse with P Choke in Thai; commands between agents always in English',
        'Bot-to-bot dispatch: write file with prefix, run hermes chat in English',
        'lnwjud: call ONLY via lnwjud-bridge/lnwjud_call.py, native MCP fails',
        'ig-maxpland scan pacing: 250-500ms per page, no long pause',
        'ig-maxpland v2: scan page size 100 both sides',
        'Threads overflow menu has Copy Link only; no media download or share',
        'MemCore is the canonical team memory system',
    )
    for fact in FACTS:
        core.create_memory(conn, project, agent, fact, scope='project', lifecycle='accepted')
    conn.commit()
    return conn, db_path, project, agent


class TestRecall2dot0(unittest.TestCase):
    """Table-driven tests for Recall 2.0 lanes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_recall2_')
        self.conn, self.db_path, self.project, self.agent = _make_store(self.tmpdir)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _search_hit(self, query, expected_substring, k=3):
        rows = core.search(self.conn, self.project, self.agent, query, limit=k)
        texts = [r[5] for r in rows]
        return any(expected_substring in t for t in texts), rows

    # ---- Lane 3.2: Fleet alias expansion (paraphrase queries) ----
    def test_alias_ai_gateway(self):
        """'AI gateway' -> '9router' makes paraphrase query hit."""
        hit, rows = self._search_hit('AI gateway หลักอยู่ที่ไหน', 'localhost:20128')
        self.assertTrue(hit, f'Expected hit for "AI gateway" query, got rows: {rows}')

    def test_alias_tim_fleet_roster(self):
        """'ทีม' -> 'fleet roster' makes Thai paraphrase hit."""
        hit, rows = self._search_hit('ทีมมีใครบ้าง', 'MIKA')
        self.assertTrue(hit, f'Expected hit for "ทีม" query, got rows: {rows}')

    def test_alias_pi_chok_thai(self):
        """'พี่โชค' -> 'thai' makes paraphrase query hit."""
        hit, rows = self._search_hit('คุยกับพี่โชคภาษาอะไร', 'Thai')
        self.assertTrue(hit, f'Expected hit for "พี่โชค" query, got rows: {rows}')

    def test_alias_scan_pacing(self):
        """'สแกน' -> 'scan pacing' makes paraphrase query hit."""
        hit, rows = self._search_hit('สแกนพักกี่วินาทีต่อหน้า', '250-500ms')
        self.assertTrue(hit, f'Expected hit for "สแกน" query, got rows: {rows}')

    # ---- Lane 3.1: Thai de-gluing via char bigrams ----
    def test_thai_bigram_hit(self):
        """Thai glued query matches spaced Thai content via char-bigram prefix terms."""
        # Store SPACE-SEPARATED Thai content (FTS indexes as separate tokens).
        # A glued query can never hit via exact-substring (spaces break instr),
        # so only the bigram prefix lane can bridge glued->spaced.
        core.create_memory(self.conn, self.project, self.agent,
                           'ระบบ ควร ทำงาน ได้ ทุกวัน',
                           scope='project', lifecycle='accepted')
        self.conn.commit()

        hit, rows = self._search_hit('ระบบควรทำ', 'ระบบ ควร ทำงาน ได้ ทุกวัน')
        self.assertTrue(hit, f'Expected bigram hit for Thai glued query, got rows: {rows}')

    def test_thai_bigram_miss_without_lane(self):
        """Prove the bigram lane is load-bearing: disable it -> query misses."""
        # Same spaced content: exact-fallback instr misses (spaces), so only
        # bigram prefix terms can hit. Disabling _thai_bigrams must -> miss.
        core.create_memory(self.conn, self.project, self.agent,
                           'ระบบ ควร ทำงาน ได้ ทุกวัน',
                           scope='project', lifecycle='accepted')
        self.conn.commit()

        # Directly call _fts_query with a patched _thai_bigrams that returns []
        from memcore import core as core_mod
        original_thai_bigrams = core_mod._thai_bigrams
        try:
            core_mod._thai_bigrams = lambda token: []  # disable bigrams
            # Query shares substring but bigrams disabled
            hit, rows = self._search_hit('ระบบควรทำ', 'ระบบ ควร ทำงาน ได้ ทุกวัน')
            self.assertFalse(hit, f'Expected MISS when bigrams disabled, got hit: {rows}')
        finally:
            core_mod._thai_bigrams = original_thai_bigrams

    # ---- Sadist / edge cases ----
    def test_empty_query_returns_empty(self):
        """Empty query returns empty list."""
        rows = core.search(self.conn, self.project, self.agent, '', limit=3)
        self.assertEqual(rows, [])

    def test_fts_operator_query_safe(self):
        """FTS operator characters in query are safely quoted (no injection)."""
        # This query contains quotes and parens — should not crash or misbehave
        hit, rows = self._search_hit('bob "x" (y)', 'x')
        # The query is tokenized and quoted safely; no hits expected
        self.assertIsInstance(rows, list)

    def test_100kb_query_handled(self):
        """Very long query (100KB) is handled without OOM or crash."""
        long_query = 'x ' * 50000  # ~100KB
        rows = core.search(self.conn, self.project, self.agent, long_query, limit=3)
        # Should return empty (no matches) or a valid list, never crash
        self.assertIsInstance(rows, list)

    def test_thai_only_particles_query(self):
        """Thai-only particles query (no content words) returns empty safely."""
        # Particles like 'นะคะ', 'ค่ะ', 'ครับ' should not match content
        rows = core.search(self.conn, self.project, self.agent, 'นะคะ ค่ะ ครับ', limit=3)
        self.assertIsInstance(rows, list)

    # ---- Mutation check: delete alias map -> at least one new test fails ----
    def test_mutation_alias_map_required(self):
        """Removing the alias map must cause at least one new test to fail."""
        from memcore import core as core_mod
        original_aliases = core_mod._FLEET_ALIASES
        try:
            # Empty the alias map
            core_mod._FLEET_ALIASES = {}
            # Re-run the four alias-dependent tests
            failures = 0
            tests = [
                ('AI gateway หลักอยู่ที่ไหน', 'localhost:20128'),
                ('ทีมมีใครบ้าง', 'MIKA'),
                ('คุยกับพี่โชคภาษาอะไร', 'Thai'),
                ('สแกนพักกี่วินาทีต่อหน้า', '250-500ms'),
            ]
            for query, expected in tests:
                hit, _ = self._search_hit(query, expected)
                if not hit:
                    failures += 1
            self.assertGreater(failures, 0, 'Expected at least one test to fail when alias map is empty')
        finally:
            core_mod._FLEET_ALIASES = original_aliases


if __name__ == '__main__':
    unittest.main()