"""Rejection hardening: pre-emptive reject-value, duplicate sweep, list/unreject.

Covers the three gaps closed without a schema change: a refusal guard can be
filed for a value with no memory row, rejecting one row sweeps live
same-claim duplicates in the same lane, and guards are listed content-free
with a prefix escape hatch (soft override, never a hard delete).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from memcore import store, core
from memcore import __main__ as cli


class RejectionHardeningBase(unittest.TestCase):
    """Temp file store + alice (owner) / bob (member) in one project."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_reject_hard_')
        self.db_path = os.path.join(self.tmpdir, 'reject.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-reject'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'reject')", (self.project,)
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


class TestPreemptiveRejectValue(RejectionHardeningBase):

    def test_preemptive_guard_blocks_later_admission_case_variant(self):
        content = 'Preemptive Bad Claim'
        out = core.reject_value(
            self.conn, self.project, self.alice, content, 'known bad')
        self.assertTrue(out['tombstone_created'])
        self.assertEqual(out['swept'], 0)
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob,
                '  preemptive   BAD claim ', scope='project')

    def test_preemptive_guard_is_idempotent(self):
        content = 'already refused value'
        first = core.reject_value(
            self.conn, self.project, self.alice, content, 'first')
        second = core.reject_value(
            self.conn, self.project, self.alice, content, 'second')
        self.assertEqual(first['tombstone_id'], second['tombstone_id'])
        self.assertFalse(second['tombstone_created'])
        n = self.conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE claim_fingerprint=? '
            'AND scope=? AND overridden_by IS NULL',
            (core.fingerprint(content), self.project)
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_preemptive_rejects_empty_content(self):
        for bad in ('', '   ', '\n\t '):
            with self.subTest(content=repr(bad)):
                with self.assertRaises(core.MemCoreError):
                    core.reject_value(
                        self.conn, self.project, self.alice, bad, 'reason')
                with self.assertRaises(core.MemCoreError):
                    core.reject(
                        self.conn, 'mem-missing', self.alice, bad)

    def test_empty_reason_refused_on_both_paths(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'needs a real reason',
            scope='project')
        for bad_reason in ('', '   '):
            with self.subTest(reason=repr(bad_reason)):
                with self.assertRaises(core.MemCoreError):
                    core.reject(self.conn, mem_id, self.alice, bad_reason)
                with self.assertRaises(core.MemCoreError):
                    core.reject_value(
                        self.conn, self.project, self.alice,
                        'some other value', bad_reason)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM tombstone').fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)).fetchone()[0],
            'candidate')

    def test_preemptive_requires_membership(self):
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) "
            "VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        with self.assertRaises(core.PermissionDenied):
            core.reject_value(
                self.conn, self.project, 'agent-mallory',
                'outsider value', 'reason')


class TestDuplicateSweep(RejectionHardeningBase):

    def test_by_id_reject_sweeps_two_live_duplicates(self):
        content = 'swept duplicate claim'
        ids = [
            core.create_memory(
                self.conn, self.project, self.alice, content, scope='project')[0]
            for _ in range(3)
        ]
        result = core.reject(self.conn, ids[0], self.alice, 'wrong')
        self.assertEqual(result['swept'], 2)
        self.assertEqual(sorted(result['swept_ids']), sorted(ids[1:]))
        lifecycles = dict(self.conn.execute(
            'SELECT id, lifecycle FROM memory').fetchall())
        self.assertTrue(all(lc == 'rejected' for lc in lifecycles.values()))
        n = self.conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE claim_fingerprint=? '
            'AND scope=? AND overridden_by IS NULL',
            (core.fingerprint(content), self.project)
        ).fetchone()[0]
        self.assertEqual(n, 1)
        swept_audits = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='reject' "
            "AND detail LIKE '%\"swept\": true%'"
        ).fetchone()[0]
        self.assertEqual(swept_audits, 2)
        # Blocked for everyone afterwards, including a whitespace variant.
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob,
                '  SWEPT duplicate CLAIM ', scope='project')

    def test_sweep_covers_candidate_accepted_conflict_only(self):
        content = 'mixed lifecycle sweep claim'
        live = [
            core.create_memory(
                self.conn, self.project, self.alice, content, scope='project')[0]
            for _ in range(3)
        ]
        self.conn.execute(
            "UPDATE memory SET lifecycle='accepted' WHERE id=?", (live[1],))
        self.conn.execute(
            "UPDATE memory SET lifecycle='conflict' WHERE id=?", (live[2],))
        disabled, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project')
        core.deactivate(self.conn, disabled, self.alice)
        self.conn.commit()
        result = core.reject(self.conn, live[0], self.alice, 'wrong')
        self.assertEqual(sorted(result['swept_ids']), sorted(live[1:]))
        self.assertEqual(
            self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (disabled,)).fetchone()[0],
            'disabled')

    def test_sweep_respects_scope_lanes(self):
        content = 'lane scoped sweep claim'
        alice_priv, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='private')
        bob_priv, _ = core.create_memory(
            self.conn, self.project, self.bob, content, scope='private')
        proj, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project')
        # Private reject sweeps only the owner's private lane.
        result = core.reject(self.conn, alice_priv, self.alice, 'alice wrong')
        self.assertEqual(result['swept'], 0)
        for mid, expected in ((bob_priv, 'candidate'), (proj, 'candidate')):
            self.assertEqual(
                self.conn.execute(
                    'SELECT lifecycle FROM memory WHERE id=?', (mid,)).fetchone()[0],
                expected)
        # Project reject sweeps project rows, never private lanes.
        result = core.reject(self.conn, proj, self.alice, 'project wrong')
        self.assertEqual(result['swept'], 0)
        self.assertEqual(
            self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (bob_priv,)).fetchone()[0],
            'candidate')
        # ... but the project guard blocks private recapture in the project.
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob, content, scope='private')

    def test_reject_value_sweeps_live_dups_and_blocks_future(self):
        content = 'preemptive sweep claim'
        first, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project')
        second, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project')
        out = core.reject_value(
            self.conn, self.project, self.alice, content, 'preemptive')
        self.assertEqual(sorted(out['swept_ids']), sorted([first, second]))
        for mid in (first, second):
            self.assertEqual(
                self.conn.execute(
                    'SELECT lifecycle FROM memory WHERE id=?', (mid,)).fetchone()[0],
                'rejected')
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob, content, scope='project')

    def test_no_match_leaves_no_memory_rows(self):
        before = self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0]
        out = core.reject_value(
            self.conn, self.project, self.alice,
            'value with no stored rows', 'preemptive')
        self.assertEqual(out['swept'], 0)
        self.assertEqual(
            self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], before)
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice,
                'value with no stored rows', scope='project')

    def test_reject_result_stays_boolean_compatible(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'bool compat claim',
            scope='project')
        self.assertTrue(core.reject(self.conn, mem_id, self.alice, 'wrong'))
        self.assertFalse(core.reject(self.conn, mem_id, self.alice, 'again'))
        self.assertEqual(core.reject(self.conn, mem_id, self.alice, 'again')['swept'], 0)


class TestListAndUnreject(RejectionHardeningBase):

    def _reject(self, content, reason='wrong'):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project')
        core.reject(self.conn, mem_id, self.alice, reason)
        return mem_id

    def test_list_is_newest_first_and_content_free(self):
        self._reject('first listed claim')
        self._reject('second listed claim')
        rows = core.list_tombstones(self.conn, self.project, self.alice)
        self.assertEqual(len(rows), 2)
        created = [r[4] for r in rows]
        self.assertTrue(all(created[i] >= created[i + 1] for i in range(len(created) - 1)))
        for row in rows:
            self.assertEqual(len(row), 5)
            tid, fp, scope, reason, _created = row
            self.assertTrue(tid.startswith('tomb-'))
            self.assertEqual(len(fp), 16)
            self.assertEqual(scope, self.project)
        blob = ' '.join(str(v) for row in rows for v in row)
        self.assertNotIn('first listed claim', blob)
        self.assertNotIn('second listed claim', blob)

    def test_list_requires_membership(self):
        self._reject('guarded claim')
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) "
            "VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        with self.assertRaises(core.PermissionDenied):
            core.list_tombstones(self.conn, self.project, 'agent-mallory')

    def test_unreject_by_exact_id_reopens_admission(self):
        content = 'unreject by id claim'
        self._reject(content)
        tomb_id = self.conn.execute(
            'SELECT id FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint(content),)).fetchone()[0]
        out = core.unreject_tombstone(self.conn, tomb_id, self.alice)
        self.assertEqual(out['tombstone_id'], tomb_id)
        self.assertTrue(out['overridden'])
        self.assertTrue(core.admission_allowed(
            self.conn, content, self.project, scope='project'))
        # Soft override only: the row survives for GC, and memories stay ended.
        self.assertEqual(
            self.conn.execute(
                'SELECT COUNT(*) FROM tombstone WHERE id=?', (tomb_id,)).fetchone()[0],
            1)
        # Second unreject is a no-op returning the same id.
        again = core.unreject_tombstone(self.conn, tomb_id, self.alice)
        self.assertEqual(again, {'tombstone_id': tomb_id, 'overridden': False})

    def test_unreject_by_unique_prefix(self):
        content = 'unreject by prefix claim'
        self._reject(content)
        fp = core.fingerprint(content)
        out = core.unreject_tombstone(self.conn, fp[:8], self.alice)
        self.assertTrue(out['overridden'])
        self.assertTrue(core.admission_allowed(
            self.conn, content, self.project, scope='project'))

    def test_unreject_unknown_raises_not_found(self):
        with self.assertRaises(core.NotFound):
            core.unreject_tombstone(self.conn, 'deadbeef', self.alice)
        with self.assertRaises(core.NotFound):
            core.unreject_tombstone(self.conn, '', self.alice)

    def test_unreject_ambiguous_prefix_lists_candidates(self):
        # Deterministic shared prefix: two guards with the same fp stem.
        for suffix in ('01', '02'):
            self.conn.execute(
                'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (core._new_id('tomb'), 'ab12cd00000000' + suffix,
                 self.project, 'ambiguous pair', core._now()),
            )
        self.conn.commit()
        with self.assertRaises(core.AmbiguousTombstonePrefix) as cm:
            core.unreject_tombstone(self.conn, 'ab12', self.alice)
        self.assertEqual(len(cm.exception.candidates), 2)

    def test_unreject_respects_override_permissions(self):
        content = 'member cannot lift project guard'
        self._reject(content)
        tomb_id = self.conn.execute(
            'SELECT id FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint(content),)).fetchone()[0]
        with self.assertRaises(core.PermissionDenied):
            core.unreject_tombstone(self.conn, tomb_id, self.bob)
        self.assertIsNotNone(core._tombstone_active(
            self.conn, core.fingerprint(content), self.project))

    def test_overridden_guard_leaves_list(self):
        content = 'overridden guard gone from list'
        self._reject(content)
        self.assertEqual(len(core.list_tombstones(
            self.conn, self.project, self.alice)), 1)
        tomb_id = self.conn.execute('SELECT id FROM tombstone').fetchone()[0]
        core.override_tombstone(self.conn, tomb_id, self.alice)
        self.assertEqual(core.list_tombstones(
            self.conn, self.project, self.alice), [])


class TestGuardRailsStillHold(RejectionHardeningBase):

    def test_guard_blocks_supersede_promote_and_idempotent_replay(self):
        claim = 'guarded lifecycle claim'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, claim,
            scope='project', idempotency_key='guard-key')
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice, claim,
                scope='project', idempotency_key='guard-key')
        other, _ = core.create_memory(
            self.conn, self.project, self.alice, 'unguarded claim', scope='private')
        with self.assertRaises(core.TombstoneBlocked):
            core.supersede(self.conn, other, self.alice, claim)
        with self.assertRaises(core.MemCoreError):
            core.promote(self.conn, mem_id, self.alice)

    def test_unchanged_row_paths_unaffected(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'plain claim', scope='project')
        self.assertTrue(core.reject(self.conn, mem_id, self.alice, 'wrong'))
        # Already-rejected reject is a reported no-op, not an error.
        self.assertFalse(core.reject(self.conn, mem_id, self.alice, 'second'))
        # Deactivate/restore roundtrip on an unguarded row still works.
        other, _ = core.create_memory(
            self.conn, self.project, self.alice, 'roundtrip claim')
        core.deactivate(self.conn, other, self.alice, reason='cleanup')
        core.restore(self.conn, other, self.alice)
        self.assertEqual(
            self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (other,)).fetchone()[0],
            'candidate')


class TestRejectValueCli(RejectionHardeningBase):

    def test_reject_value_and_tombstone_cli_roundtrip(self):
        import contextlib
        import io
        conn = store.open_store(self.db_path)
        conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-owner', 'owner', 'owner')")
        conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role) "
            "VALUES (?, 'agent-owner', 'owner')", (self.project,))
        conn.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['--db', self.db_path, 'reject-value',
                      '--project', 'reject', '--agent', 'owner',
                      '--reason', 'cli preemptive', 'cli preemptive claim'])
        self.assertIn('rejected value + tombstoned', out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['tombstone', 'list', '--db', self.db_path,
                      '--project', 'reject', '--agent', 'owner'])
        self.assertNotIn('cli preemptive claim', out.getvalue())
        self.assertIn('fp:', out.getvalue())
        fp = core.fingerprint('cli preemptive claim')
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['tombstone', 'unreject', '--db', self.db_path,
                      fp[:8], '--agent', 'owner'])
        self.assertIn('unrejected', out.getvalue())


if __name__ == '__main__':
    unittest.main()
