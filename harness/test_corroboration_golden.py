"""v0.7 autonomy tests: corroboration → accept → Golden Rule, decay.

Covers ADR-0018 (N=3 accept, N=5 golden, tombstone veto, supersede reset)
and core.apply_freshness_decay.
"""
import os
import tempfile
import unittest

from memcore import core, store


class CorroborationBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_corrob_')
        self.db_path = os.path.join(self.tmpdir, 'corrob.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-demo'
        self.agents = [f'agent-{n}' for n in
                       ('alice', 'bob', 'cara', 'dave', 'erin')]
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'demo')",
            (self.project,))
        for i, aid in enumerate(self.agents):
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, aid.removeprefix('agent-'), aid.removeprefix('agent-')))
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

    def remember(self, agent, content, scope='private'):
        mem_id, _ = core.create_memory(
            self.conn, self.project, agent, content, scope=scope)
        return mem_id


class TestCorroborationAccept(CorroborationBase):
    CLAIM = 'deployments require the migration step first'

    def test_two_sources_stay_candidate(self):
        ids = [self.remember(a, self.CLAIM) for a in self.agents[:2]]
        fp = core.fingerprint(self.CLAIM)
        out = core.maybe_auto_corrob(
            self.conn, self.project, fp, self.agents[0])
        self.assertEqual(out['sources'], 2)
        self.assertEqual(out['action'], 'none')
        for mid in ids:
            lc = self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mid,)).fetchone()[0]
            self.assertEqual(lc, 'candidate')

    def test_three_sources_auto_accept_canonical(self):
        ids = [self.remember(a, self.CLAIM) for a in self.agents[:3]]
        fp = core.fingerprint(self.CLAIM)
        out = core.maybe_auto_corrob(
            self.conn, self.project, fp, self.agents[0])
        self.assertEqual(out['sources'], 3)
        self.assertEqual(out['action'], 'accepted')
        canon = out['canonical']
        self.assertIn(canon, ids)
        row = self.conn.execute(
            'SELECT scope, lifecycle, verification FROM memory WHERE id=?',
            (canon,)).fetchone()
        self.assertEqual(row, ('project', 'accepted', 'source_backed'))
        audit = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='auto_corrob_accept' "
            'AND memory_id=?', (canon,)).fetchone()[0]
        self.assertEqual(audit, 1)

    def test_five_sources_crown_golden(self):
        [self.remember(a, self.CLAIM) for a in self.agents[:5]]
        fp = core.fingerprint(self.CLAIM)
        out = core.maybe_auto_corrob(
            self.conn, self.project, fp, self.agents[0])
        self.assertEqual(out['sources'], 5)
        self.assertEqual(out['action'], 'golden')
        row = self.conn.execute(
            'SELECT lifecycle, pinned, critical FROM memory WHERE id=?',
            (out['canonical'],)).fetchone()
        self.assertEqual(row, ('accepted', 1, 1))
        audit = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='auto_golden_promote' "
            'AND memory_id=?', (out['canonical'],)).fetchone()[0]
        self.assertEqual(audit, 1)

    def test_same_agent_copies_do_not_count(self):
        for _ in range(4):
            core.create_memory(
                self.conn, self.project, self.agents[0], self.CLAIM,
                scope='private',
                idempotency_key=f'k-{_}-{os.urandom(2).hex()}')
        fp = core.fingerprint(self.CLAIM)
        out = core.maybe_auto_corrob(
            self.conn, self.project, fp, self.agents[0])
        self.assertEqual(out['sources'], 1)
        self.assertEqual(out['action'], 'none')

    def test_tombstone_veto_wins(self):
        # Project-scope rejection tombstones the project guard — the same
        # claim from any other agent must never auto-promote afterwards.
        proj_ids = [
            self.remember(a, self.CLAIM, scope='project')
            for a in self.agents[:2]]
        self.remember(self.agents[2], self.CLAIM)
        core.reject(self.conn, proj_ids[0], self.agents[0], 'wrong claim')
        fp = core.fingerprint(self.CLAIM)
        out = core.maybe_auto_corrob(
            self.conn, self.project, fp, self.agents[1])
        self.assertEqual(out['action'], 'vetoed')

    def test_supersede_resets_the_count(self):
        ids = [self.remember(a, self.CLAIM) for a in self.agents[:2]]
        core.supersede(
            self.conn, ids[0], self.agents[0],
            'deployments require the checklist step first',
            reason='correction')
        fp = core.fingerprint(self.CLAIM)
        out = core.maybe_auto_corrob(
            self.conn, self.project, fp, self.agents[1])
        # Corrected copy now tombstones the old claim; sweep must not promote.
        self.assertIn(out['action'], ('none', 'vetoed'))
        self.assertLessEqual(out['sources'], 2)


class TestGoldenHelpers(CorroborationBase):
    def test_set_golden_and_ungolden_roundtrip(self):
        mid = self.remember(self.agents[0], 'a durable operational rule')
        core.set_golden(self.conn, mid, self.agents[0], True)
        row = self.conn.execute(
            'SELECT pinned, critical FROM memory WHERE id=?', (mid,)).fetchone()
        self.assertEqual(row, (1, 1))
        core.set_golden(self.conn, mid, self.agents[0], False)
        row = self.conn.execute(
            'SELECT pinned, critical FROM memory WHERE id=?', (mid,)).fetchone()
        self.assertEqual(row, (0, 0))

    def test_accept_memory_rejects_terminal(self):
        mid = self.remember(self.agents[0], 'a mistaken claim')
        core.reject(self.conn, mid, self.agents[0], 'wrong')
        with self.assertRaises(core.MemCoreError):
            core.accept_memory(self.conn, mid, self.agents[0], 'too late')


class TestFreshnessDecay(CorroborationBase):
    def test_decay_ages_then_stales_without_touching_lifecycle(self):
        mid = self.remember(self.agents[0], 'an aging operational note')
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-45 days') WHERE id=?",
            (mid,))
        self.conn.commit()
        aged, staled = core.apply_freshness_decay(
            self.conn, aging_days=30, stale_days=90)
        self.assertIn(mid, aged)
        self.assertEqual(staled, [])
        row = self.conn.execute(
            'SELECT freshness, lifecycle FROM memory WHERE id=?',
            (mid,)).fetchone()
        self.assertEqual(row, ('aging', 'candidate'))
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-100 days') WHERE id=?",
            (mid,))
        self.conn.commit()
        aged2, staled2 = core.apply_freshness_decay(
            self.conn, aging_days=30, stale_days=90)
        self.assertIn(mid, staled2)
        row = self.conn.execute(
            'SELECT freshness, lifecycle FROM memory WHERE id=?',
            (mid,)).fetchone()
        self.assertEqual(row, ('stale', 'candidate'))

    def test_decay_rejects_inverted_windows(self):
        with self.assertRaises(core.MemCoreError):
            core.apply_freshness_decay(
                self.conn, aging_days=90, stale_days=30)


if __name__ == '__main__':
    unittest.main()
