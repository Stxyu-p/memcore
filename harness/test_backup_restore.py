"""Backup / restore / recovery-readiness contract tests.

These pin the behaviour the 2026-10-03 zero-fill incident made necessary:
a store must have a verified recovery path, and the tooling that creates and
uses one must itself be trustworthy.
"""
import contextlib
import io
import os
import pathlib
import sqlite3
import tempfile
import time
import unittest

from memcore import core, store


class BackupBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_backup_')
        self.db_path = os.path.join(self.tmpdir, 'memory.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-backup'
        self.agent = 'agent-tester'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'backup')", (self.project,)
        )
        self.conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (self.agent, 'tester', 'tester'),
        )
        self.conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)', (self.project, self.agent, 'owner'),
        )
        self.conn.commit()

    def tearDown(self):
        try:
            if self.conn is not None:
                self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def remember(self, content):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.agent, content, scope='private',
        )
        return mem_id

    def close_store(self):
        """Close the connection and fold the WAL back in, releasing sidecars.

        A closed WAL-mode connection still holds its -shm handle on Windows, so
        a test that replaces the file needs the checkpoint first.
        """
        if self.conn is not None:
            try:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        for suffix in ('-wal', '-shm'):
            path = pathlib.Path(self.db_path + suffix)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass

    def backup_dir(self):
        return pathlib.Path(self.db_path).parent / store.BACKUP_DIRNAME

    def age(self, path, days):
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))


class TestBackupCreation(BackupBase):
    def test_backup_writes_verified_snapshot(self):
        self.remember('snapshot must carry this row')
        dest = store.backup_store(self.db_path)
        self.assertTrue(dest.is_file())
        self.assertEqual(dest.parent.name, store.BACKUP_DIRNAME)
        check = sqlite3.connect(str(dest))
        try:
            self.assertEqual(
                check.execute('PRAGMA integrity_check').fetchone()[0], 'ok'
            )
            self.assertEqual(
                check.execute(
                    'SELECT COUNT(*) FROM memory_version WHERE content=?',
                    ('snapshot must carry this row',),
                ).fetchone()[0],
                1,
            )
        finally:
            check.close()

    def test_backup_rapid_collision_guard_preserves_all_snapshots(self):
        """Rapid backup_store calls in the same second must not overwrite each other."""
        self.remember('rapid backup 1')
        b1 = store.backup_store(self.db_path)
        self.remember('rapid backup 2')
        b2 = store.backup_store(self.db_path)
        self.remember('rapid backup 3')
        b3 = store.backup_store(self.db_path)
        self.assertNotEqual(b1, b2)
        self.assertNotEqual(b2, b3)
        self.assertTrue(b1.is_file())
        self.assertTrue(b2.is_file())
        self.assertTrue(b3.is_file())
        # All 3 files must be recognized as managed snapshots
        managed = store._managed_snapshots(b1.parent, pathlib.Path(self.db_path).stem)
        managed_names = {p.name for p in managed}
        self.assertIn(b1.name, managed_names)
        self.assertIn(b2.name, managed_names)
        self.assertIn(b3.name, managed_names)

    def test_backup_is_self_contained_without_sidecars(self):
        """A snapshot must not depend on the source's WAL sidecars."""
        self.remember('self contained probe')
        dest = store.backup_store(self.db_path)
        # The snapshot must stand alone: drop the source's sidecars, then read
        # it in a fresh connection far from the source.
        self.close_store()
        for suffix in ('-wal', '-shm'):
            sidecar = pathlib.Path(self.db_path + suffix)
            if sidecar.exists():
                sidecar.unlink()
        check = sqlite3.connect(str(dest))
        try:
            self.assertEqual(
                check.execute('PRAGMA integrity_check').fetchone()[0], 'ok'
            )
        finally:
            check.close()

    def test_backup_leaves_no_partial_file(self):
        store.backup_store(self.db_path)
        self.assertEqual(list(self.backup_dir().glob('*.partial')), [])

    def test_backup_refuses_missing_store(self):
        with self.assertRaises(store.StoreError):
            store.backup_store(os.path.join(self.tmpdir, 'absent.db'))

    def test_backup_prunes_to_keep_count(self):
        for _ in range(4):
            dest = store.backup_store(self.db_path, keep=2)
            os.utime(dest, (time.time(), time.time()))
            time.sleep(1.01)
        remaining = store._managed_snapshots(
            self.backup_dir(), pathlib.Path(self.db_path).stem
        )
        self.assertEqual(len(remaining), 2)

    def test_prune_never_deletes_foreign_files(self):
        self.backup_dir().mkdir(parents=True, exist_ok=True)
        foreign = self.backup_dir() / 'memory-pre-manual-20260101.db'
        foreign.write_bytes(b'hand placed backup')
        store.backup_store(self.db_path, keep=1)
        self.assertTrue(foreign.exists())


class TestBackupReporting(BackupBase):
    def test_fresh_snapshot_means_recovery_ready(self):
        store.backup_store(self.db_path)
        report = store.verify_backups(self.db_path)
        self.assertTrue(report['recovery_ready'])
        self.assertEqual(report['snapshot_count'], 1)
        self.assertEqual(report['newest_age_days'], 0.0)

    def test_no_snapshot_means_not_ready(self):
        self.backup_dir().mkdir(parents=True, exist_ok=True)
        report = store.verify_backups(self.db_path)
        self.assertFalse(report['recovery_ready'])
        self.assertIn('no_snapshots', report['problems'])

    def test_stale_snapshot_means_not_ready(self):
        dest = store.backup_store(self.db_path)
        self.age(dest, days=30)
        report = store.verify_backups(self.db_path, max_age_days=7)
        self.assertFalse(report['recovery_ready'])
        self.assertTrue(
            any(p.startswith('stale_backup:') for p in report['problems'])
        )

    def test_single_snapshot_counts_as_recovery_path(self):
        """One verified snapshot is a recovery path; count is depth."""
        store.backup_store(self.db_path)
        report = store.verify_backups(self.db_path)
        self.assertTrue(report['recovery_ready'])
        self.assertIn('below_target_snapshots:1', report['problems'])

    def test_foreign_files_are_not_counted_as_recovery_points(self):
        self.backup_dir().mkdir(parents=True, exist_ok=True)
        (self.backup_dir() / 'memory-handmade-20260101T000000Z.db').write_bytes(b'junk')
        report = store.verify_backups(self.db_path)
        self.assertFalse(report['recovery_ready'])

    def test_missing_backup_dir_is_reported(self):
        elsewhere = os.path.join(self.tmpdir, 'elsewhere', 'memory.db')
        report = store.verify_backups(elsewhere)
        self.assertFalse(report['recovery_ready'])
        self.assertIn('no_backup_dir', report['problems'])

    def test_newest_is_chosen_by_mtime_not_filename(self):
        first = store.backup_store(self.db_path)
        time.sleep(1.01)  # filename stamps have 1s resolution
        second = store.backup_store(self.db_path)
        self.age(first, days=5)
        report = store.verify_backups(self.db_path)
        self.assertEqual(report['newest_snapshot'], second.name)
        self.assertLess(report['newest_age_days'], report['oldest_age_days'])

    def test_report_never_contains_memory_text(self):
        self.remember('SECRETCANARY payload must never leak')
        store.backup_store(self.db_path)
        self.assertNotIn('SECRETCANARY', str(store.verify_backups(self.db_path)))


class TestRestore(BackupBase):
    def _run(self, snapshot, confirm):
        from memcore.__main__ import main
        argv = ['--db', self.db_path,
                'restore-from-snapshot', '--snapshot', str(snapshot)]
        if confirm:
            argv.append('--confirm')
        main(argv)

    def test_preview_writes_nothing(self):
        snapshot = store.backup_store(self.db_path)
        before = pathlib.Path(self.db_path).read_bytes()
        self._run(snapshot, confirm=False)
        self.assertEqual(pathlib.Path(self.db_path).read_bytes(), before)

    def test_restore_preserves_current_store(self):
        snapshot = store.backup_store(self.db_path)
        self.close_store()
        self._run(snapshot, confirm=True)
        preserved = list(
            pathlib.Path(self.tmpdir).glob('memory.db.pre-restore-*.bak')
        )
        self.assertEqual(len(preserved), 1)

    def test_restore_rejects_non_database_snapshot(self):
        bad = pathlib.Path(self.tmpdir) / 'notadb.db'
        bad.write_bytes(b'this is definitely not a sqlite file')
        with self.assertRaises(SystemExit):
            self._run(bad, confirm=True)

    def test_restore_removes_stale_sidecars(self):
        """A leftover -wal from the replaced file would graft foreign frames."""
        snapshot = store.backup_store(self.db_path)
        self.close_store()  # release the WAL so the file can be removed
        wal = pathlib.Path(self.db_path + '-wal')
        wal.write_bytes(b'stale wal frames')
        self._run(snapshot, confirm=True)
        self.assertFalse(wal.exists())

    def test_restore_refuses_while_a_live_connection_holds_the_store(self):
        """A locked sidecar must abort loudly, never graft foreign frames."""
        snapshot = store.backup_store(self.db_path)
        with self.assertRaises(SystemExit):
            self._run(snapshot, confirm=True)
        # the real store must be untouched by the aborted attempt
        conn = store.open_store_readonly(self.db_path)
        try:
            self.assertEqual(
                conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok'
            )
        finally:
            conn.close()

    def test_restored_store_is_readable_and_clean(self):
        self.remember('row that must survive the restore')
        snapshot = store.backup_store(self.db_path)
        self.close_store()
        self._run(snapshot, confirm=True)
        conn = store.open_store_readonly(self.db_path)
        try:
            self.assertEqual(
                conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok'
            )
            self.assertEqual(
                conn.execute(
                    'SELECT COUNT(*) FROM memory_version WHERE content=?',
                    ('row that must survive the restore',),
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_restore_rejects_missing_snapshot(self):
        with self.assertRaises(SystemExit):
            self._run(pathlib.Path(self.tmpdir) / 'ghost.db', confirm=True)


class TestProvenanceSeal(unittest.TestCase):
    """Phase 6a: tamper-evident seal on journal writes."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_prov_')
        self.db_path = os.path.join(self.tmpdir, 'prov.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-prov'
        self.agent = 'agent-prov'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'prov')", (self.project,)
        )
        self.conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (self.agent, 'prov', 'prov'),
        )
        self.conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)', (self.project, self.agent, 'owner'),
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

    def _event(self, user='provenance probe turn'):
        from memcore import ingest as _ingest
        eid, _ = _ingest.append_event(
            self.conn, self.project, self.agent, 'turn',
            session_id='prov-1', user_content=user,
            assistant_content='ack',
        )
        return eid

    def test_new_events_carry_valid_seal(self):
        eid = self._event()
        result = store.verify_event_seal(self.conn, eid)
        self.assertTrue(result['sealed'])
        self.assertTrue(result['valid'])

    def test_tampered_attribution_fails_verification(self):
        # FK guards agent_id, so simulate post-write tampering with FK off —
        # exactly the kind of direct-file edit the seal exists to catch.
        eid = self._event()
        self.conn.execute('PRAGMA foreign_keys = OFF')
        try:
            self.conn.execute(
                "UPDATE ingest_event SET agent_id='agent-impostor' WHERE id=?",
                (eid,),
            )
            self.conn.commit()
        finally:
            self.conn.execute('PRAGMA foreign_keys = ON')
        result = store.verify_event_seal(self.conn, eid)
        self.assertTrue(result['sealed'])
        self.assertFalse(result['valid'])

    def test_tampered_content_hash_fails_verification(self):
        eid = self._event()
        self.conn.execute(
            "UPDATE ingest_event SET content_hash='deadbeef' WHERE id=?",
            (eid,),
        )
        self.conn.commit()
        self.assertFalse(store.verify_event_seal(self.conn, eid)['valid'])

    def test_census_counts_without_reading_text(self):
        self._event(user='SECRETCANARY seal census probe')
        report = store.verify_all_seals(self.conn)
        self.assertEqual(report['checked'], 1)
        self.assertEqual(report['valid'], 1)
        self.assertEqual(report['invalid'], 0)
        self.assertNotIn('SECRETCANARY', str(report))

    def test_seal_survives_content_edit_but_attribution_change_does_not(self):
        # Editing body text does not break the seal (seal covers the
        # attribution triple, not the body); changing who-wrote-it does.
        eid = self._event()
        self.conn.execute(
            "UPDATE ingest_event SET user_content='edited body' WHERE id=?",
            (eid,),
        )
        self.conn.commit()
        self.assertTrue(store.verify_event_seal(self.conn, eid)['valid'])


class TestReinforcementDecay(unittest.TestCase):
    """Phase 6b: used facts resist decay, unused facts fade."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_reinf_')
        self.db_path = os.path.join(self.tmpdir, 'reinf.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-reinf'
        self.agent = 'agent-reinf'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'reinf')", (self.project,)
        )
        self.conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (self.agent, 'reinf', 'reinf'),
        )
        self.conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)', (self.project, self.agent, 'owner'),
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

    def _old_memory(self, content, updated_days_ago=60):
        from memcore import core as _core
        mem_id, _ = _core.create_memory(
            self.conn, self.project, self.agent, content, scope='project')
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-' || ? || ' days') "
            'WHERE id=?', (updated_days_ago, mem_id))
        self.conn.commit()
        return mem_id

    def test_unused_old_memory_ages(self):
        from memcore import core as _core
        mem_id = self._old_memory('unrecalled old claim')
        aged, _ = _core.apply_freshness_decay(self.conn)
        self.assertIn(mem_id, aged)

    def test_recently_recalled_memory_resists_decay(self):
        from memcore import core as _core
        mem_id = self._old_memory('recalled old claim')
        self.assertEqual(_core.record_recall(self.conn, [mem_id]), 1)
        aged, _ = _core.apply_freshness_decay(self.conn)
        self.assertNotIn(mem_id, aged)
        freshness = self.conn.execute(
            'SELECT freshness FROM memory WHERE id=?', (mem_id,)).fetchone()[0]
        self.assertEqual(freshness, 'current')

    def test_recall_count_increments(self):
        from memcore import core as _core
        mem_id = self._old_memory('counted claim')
        _core.record_recall(self.conn, [mem_id])
        _core.record_recall(self.conn, [mem_id])
        count = self.conn.execute(
            'SELECT recall_count FROM memory WHERE id=?',
            (mem_id,)).fetchone()[0]
        self.assertEqual(count, 2)

    def test_record_recall_never_raises(self):
        from memcore import core as _core
        # Unknown ids and empty lists are no-ops, not errors.
        self.assertEqual(_core.record_recall(self.conn, []), 0)
        self.assertEqual(_core.record_recall(self.conn, ['ghost-id']), 0)

    def test_reinforcement_window_expires(self):
        from memcore import core as _core
        mem_id = self._old_memory('stale reinforcement claim')
        _core.record_recall(self.conn, [mem_id])
        # Backdate the recall beyond the window: protection lapses.
        self.conn.execute(
            "UPDATE memory SET last_recalled=datetime('now', '-30 days') "
            'WHERE id=?', (mem_id,))
        self.conn.commit()
        aged, _ = _core.apply_freshness_decay(self.conn)
        self.assertIn(mem_id, aged)


class TestContradictionSweep(unittest.TestCase):
    """Phase 6c: same subject + opposite polarity proposes, never resolves."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_contra_')
        self.db_path = os.path.join(self.tmpdir, 'contra.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-contra'
        self.agent = 'agent-contra'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'contra')", (self.project,)
        )
        self.conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (self.agent, 'contra', 'contra'),
        )
        self.conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)', (self.project, self.agent, 'owner'),
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

    def _add(self, content, lifecycle='accepted'):
        from memcore import core as _core
        mem_id, _ = _core.create_memory(
            self.conn, self.project, self.agent, content,
            scope='project', lifecycle=lifecycle)
        return mem_id

    def test_polarity_pair_is_found(self):
        from memcore import core as _core
        # Candidates: an 'accepted' claim that contradicts live memory is now
        # held as conflict at create time (ADR: single accept choke point).
        a = self._add('scan pacing uses 250ms per page', 'candidate')
        b = self._add('scan pacing does not use 250ms per page', 'candidate')
        pairs = _core.scan_contradictions(self.conn, self.project)
        found = {(x, y) for x, y, _ in pairs} | {(y, x) for x, y, _ in pairs}
        self.assertIn((a, b), found)

    def test_thai_negation_pair_is_found(self):
        from memcore import core as _core
        a = self._add('ต่อไปนี้ใช้ B.AI สำหรับงานนี้', 'candidate')
        b = self._add('ห้ามใช้ B.AI สำหรับงานนี้', 'candidate')
        pairs = _core.scan_contradictions(self.conn, self.project)
        found = {(x, y) for x, y, _ in pairs} | {(y, x) for x, y, _ in pairs}
        self.assertIn((a, b), found)

    def test_numeric_disagreement_is_found(self):
        from memcore import core as _core
        a = self._add('gateway port is 20128', 'candidate')
        b = self._add('gateway port is 8080', 'candidate')
        pairs = _core.scan_contradictions(self.conn, self.project)
        found = {(x, y) for x, y, _ in pairs} | {(y, x) for x, y, _ in pairs}
        self.assertIn((a, b), found)

    def test_agreeing_claims_are_not_flagged(self):
        from memcore import core as _core
        self._add('gateway port is 20128')
        self._add('gateway answers at localhost:20128')
        self.assertEqual(
            _core.scan_contradictions(self.conn, self.project), [])

    def test_marking_requires_confirm_and_is_reversible(self):
        from memcore import core as _core
        a = self._add('scan pacing uses 250ms per page', 'candidate')
        b = self._add('scan pacing does not use 250ms per page', 'candidate')
        _core.mark_contradiction(
            self.conn, a, b, self.agent, reason='test pair')
        for mem_id in (a, b):
            lc = self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?',
                (mem_id,)).fetchone()[0]
            self.assertEqual(lc, 'conflict')
        # reversible via supersede
        _core.supersede(self.conn, a, self.agent, 'scan pacing uses 300ms')
        lc = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (a,)).fetchone()[0]
        self.assertEqual(lc, 'candidate')

    def test_marking_terminal_memory_refuses(self):
        from memcore import core as _core
        a = self._add('terminal claim one')
        b = self._add('terminal claim two')
        _core.reject(self.conn, a, self.agent, reason='wrong')
        with self.assertRaises(_core.MemCoreError):
            _core.mark_contradiction(
                self.conn, a, b, self.agent, reason='test pair')


class TestScopeDetail(unittest.TestCase):
    """Phase 6d: scope_detail subdivides private scope; governance untouched."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_scoped_')
        self.db_path = os.path.join(self.tmpdir, 'scoped.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-scoped'
        self.agent = 'agent-scoped'
        self.other = 'agent-other'
        for aid in (self.agent, self.other):
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, aid.removeprefix('agent-'), aid.removeprefix('agent-')),
            )
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'scoped')", (self.project,)
        )
        for aid in (self.agent, self.other):
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)', (self.project, aid, 'member'),
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

    def test_tagged_private_memory_filters_by_detail(self):
        from memcore import core as _core
        _core.create_memory(
            self.conn, self.project, self.agent, 'skill tagged probe alpha',
            scope='private', scope_detail='skill:alpha')
        _core.create_memory(
            self.conn, self.project, self.agent, 'skill tagged probe beta',
            scope='private', scope_detail='skill:beta')
        hits = _core.search(
            self.conn, self.project, self.agent, 'probe',
            scope_detail='skill:alpha')
        self.assertEqual(len(hits), 1)
        self.assertIn('alpha', hits[0][5])

    def test_untagged_search_sees_everything(self):
        from memcore import core as _core
        _core.create_memory(
            self.conn, self.project, self.agent, 'detail visibility probe',
            scope='private', scope_detail='skill:x')
        _core.create_memory(
            self.conn, self.project, self.agent, 'detail visibility probe',
            scope='private')
        hits = _core.search(self.conn, self.project, self.agent, 'probe')
        self.assertEqual(len(hits), 2)

    def test_detail_does_not_widen_access(self):
        from memcore import core as _core
        _core.create_memory(
            self.conn, self.project, self.agent, 'owner only detail probe',
            scope='private', scope_detail='skill:x')
        hits = _core.search(self.conn, self.project, self.other, 'probe')
        self.assertEqual(hits, [])

    def test_project_scope_rejects_detail(self):
        from memcore import core as _core
        with self.assertRaises(_core.MemCoreError):
            _core.create_memory(
                self.conn, self.project, self.agent, 'project detail probe',
                scope='project', scope_detail='skill:x')

    def test_invalid_prefix_rejected(self):
        from memcore import core as _core
        with self.assertRaises(_core.MemCoreError):
            _core.create_memory(
                self.conn, self.project, self.agent, 'bad prefix probe',
                scope='private', scope_detail='bogus:x')


class TestBitemporalRead(unittest.TestCase):
    """Phase 6e: point-in-time reads over closed validity windows."""

    def setUp(self):
        import tempfile, time
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_bitemp_')
        self.db_path = os.path.join(self.tmpdir, 'bitemp.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-bitemp'
        self.agent = 'agent-bitemp'
        self.other = 'agent-reader'
        for aid in (self.agent, self.other):
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, aid.removeprefix('agent-'), aid.removeprefix('agent-')),
            )
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'bitemp')", (self.project,)
        )
        for aid in (self.agent, self.other):
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)', (self.project, aid, 'member'),
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

    def test_supersede_closes_old_window(self):
        from memcore import core as _core
        mem_id, v1 = _core.create_memory(
            self.conn, self.project, self.agent, 'original bitemporal claim',
            scope='project')
        _core.supersede(self.conn, mem_id, self.agent, 'revised bitemporal claim')
        row = self.conn.execute(
            'SELECT valid_from, valid_until FROM memory_version WHERE id=?',
            (v1,)).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIsNotNone(row[1])

    def test_version_at_returns_old_content_before_supersede(self):
        import time
        from memcore import core as _core
        mem_id, v1 = _core.create_memory(
            self.conn, self.project, self.agent, 'bitemporal before claim',
            scope='project')
        before = self.conn.execute(
            'SELECT created_at FROM memory_version WHERE id=?',
            (v1,)).fetchone()[0]
        time.sleep(1.01)  # ISO timestamps have 1s resolution
        _core.supersede(self.conn, mem_id, self.agent, 'bitemporal after claim')
        row = _core.version_at(self.conn, mem_id, self.agent, before)
        self.assertIsNotNone(row)
        self.assertIn('before', row[1])
        now_row = _core.version_at(
            self.conn, mem_id, self.agent, '2999-01-01T00:00:00Z')
        self.assertIn('after', now_row[1])

    def test_version_at_respects_scope(self):
        from memcore import core as _core
        mem_id, _ = _core.create_memory(
            self.conn, self.project, self.agent, 'private bitemporal claim',
            scope='private')
        row = _core.version_at(
            self.conn, mem_id, self.other, '2999-01-01T00:00:00Z')
        self.assertIsNone(row)

    def test_version_at_unknown_memory_is_none(self):
        from memcore import core as _core
        self.assertIsNone(
            _core.version_at(
                self.conn, 'mem-ghost', self.agent, '2999-01-01T00:00:00Z'))


class TestZeroFillGuard(unittest.TestCase):
    """open_store must refuse an externally-damaged file with a clear error.

    Regression for the 2026-10-03 incident: memory.db was found 96.6% zeroed
    with a valid header. SQLite opened it happily and every query then failed
    with "database disk image is malformed". The guard below fails fast at
    open time and points at the restore path instead.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_zerofill_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_pages(self, name, pages):
        path = os.path.join(self.tmpdir, name)
        with open(path, 'wb') as fh:
            for page in pages:
                fh.write(page)
        return path

    def test_healthy_store_opens(self):
        db = os.path.join(self.tmpdir, 'healthy.db')
        conn = store.open_store(db)
        conn.close()
        conn = store.open_store(db)
        conn.close()

    def test_zero_filled_file_is_refused(self):
        header = bytearray(100)
        header[0:16] = b'SQLite format 3\x00'
        header[16:18] = (1).to_bytes(2, 'big')  # page_size 4096 (1 == 65536? no: use 4096)
        import struct
        header[16:18] = struct.pack('>H', 4096)
        header[28:32] = struct.pack('>I', 70)
        page1 = bytes(header) + bytes(4096 - 100)
        path = self._write_pages('zero.db', [page1] + [bytes(4096)] * 69)
        with self.assertRaises(store.StoreError) as cm:
            store.open_store(path)
        self.assertIn('externally damaged', str(cm.exception))
        self.assertIn('restore-from-snapshot', str(cm.exception))

    def test_missing_file_still_creates(self):
        db = os.path.join(self.tmpdir, 'new.db')
        conn = store.open_store(db)
        try:
            self.assertEqual(
                conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok'
            )
        finally:
            conn.close()

    def test_empty_file_still_creates(self):
        db = os.path.join(self.tmpdir, 'empty.db')
        pathlib.Path(db).write_bytes(b'')
        conn = store.open_store(db)
        try:
            self.assertEqual(
                conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok'
            )
        finally:
            conn.close()


class TestDoctorGatesOnBackups(unittest.TestCase):
    """doctor output must expose recovery state."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_doctor_backup_')
        self.db_path = os.path.join(self.tmpdir, 'memory.db')

    def tearDown(self):
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _doctor(self):
        from memcore.__main__ import main
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                main(['--db', self.db_path, 'doctor'])
        except SystemExit as e:
            # doctor exits 1 when any check fails; that IS the behaviour under
            # test here, so capture it rather than propagating it.
            buf.write(f'\nexit_code={e.code}')
        return buf.getvalue()

    def _seed(self):
        conn = store.open_store(self.db_path)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-x', 'x')")
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        conn.close()
        for suffix in ('-wal', '-shm'):
            path = pathlib.Path(self.db_path + suffix)
            if path.exists():
                path.unlink()

    def test_doctor_reports_missing_backups(self):
        self._seed()
        out = self._doctor()
        self.assertIn('backups:', out)
        self.assertIn('recovery_ready=False', out)
        self.assertTrue(
            'no_snapshots' in out or 'no_backup_dir' in out,
            f'expected a missing-backup reason, got: {out}',
        )

    def test_doctor_reports_ready_after_backup(self):
        self._seed()
        store.backup_store(self.db_path)
        out = self._doctor()
        self.assertIn('recovery_ready=True', out)

    def test_doctor_gates_on_a_store_without_recovery_path(self):
        """A store with no backup must not report healthy overall."""
        self._seed()
        out = self._doctor()
        self.assertIn('exit_code=1', out)

    def test_backup_line_absent_before_this_change_is_impossible(self):
        """Guards the gate itself: recovery_ready=False must fail doctor."""
        self._seed()
        dest = store.backup_store(self.db_path)
        stamp = time.time() - 30 * 86400
        os.utime(dest, (stamp, stamp))
        out = self._doctor()
        self.assertIn('recovery_ready=False', out)
        self.assertIn('exit_code=1', out)


if __name__ == '__main__':
    unittest.main()


class TestCorroborationFunnel(unittest.TestCase):
    """The number that tells whether ADR-0018 can ever fire."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_funnel_')
        self.db_path = os.path.join(self.tmpdir, 'memory.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-funnel'
        for suffix in ('', '-wal', '-shm'):
            pass  # cleanup handles removal below

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

    def _agents(self, *names):
        for name in names:
            aid = f'agent-{name}'
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, name, name),
            )
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'funnel')", (self.project,)
        )
        for name in names:
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)', (self.project, f'agent-{name}', 'member'),
            )
        self.conn.commit()

    def test_funnel_counts_distinct_writers(self):
        self._agents('a', 'b', 'c')
        core.create_memory(self.conn, self.project, 'agent-a', 'shared claim', scope='private')
        core.create_memory(self.conn, self.project, 'agent-b', 'shared claim', scope='private')
        core.create_memory(self.conn, self.project, 'agent-c', 'lone claim', scope='private')
        funnel = store.corroboration_funnel(self.conn)
        self.assertEqual(funnel['fingerprints'], 2)
        self.assertEqual(funnel['at_1'], 1)
        self.assertEqual(funnel['at_2'], 1)
        self.assertEqual(funnel['at_accept'], 0)

    def test_funnel_sees_reachable_accept(self):
        self._agents('a', 'b', 'c')
        for agent in ('agent-a', 'agent-b', 'agent-c'):
            core.create_memory(self.conn, self.project, agent, 'same claim', scope='private')
        funnel = store.corroboration_funnel(self.conn)
        self.assertTrue(funnel['reachable'])
        self.assertEqual(funnel['at_accept'], 1)

    def test_tombstoned_copies_still_count_but_cannot_promote(self):
        """Funnel counts live rows; promotion is vetoed elsewhere. The two must agree."""
        self._agents('a', 'b', 'c')
        for agent in ('agent-a', 'agent-b', 'agent-c'):
            core.create_memory(self.conn, self.project, agent, 'doomed claim', scope='private')
        funnel = store.corroboration_funnel(self.conn)
        self.assertEqual(funnel['at_accept'], 1)
        fp = core.fingerprint('doomed claim')
        outcome = core.maybe_auto_corrob(self.conn, self.project, fp, 'agent-a')
        self.assertEqual(outcome['action'], 'accepted')
        # a tombstone filed afterwards must veto the next sweep
        core.reject(self.conn, outcome['canonical'], 'agent-a', reason='wrong')
        outcome2 = core.maybe_auto_corrob(self.conn, self.project, fp, 'agent-a')
        self.assertEqual(outcome2['action'], 'vetoed')

    def test_funnel_never_contains_memory_text(self):
        self._agents('a')
        core.create_memory(
            self.conn, self.project, 'agent-a',
            'SECRETCANARY funnel content probe', scope='private',
        )
        self.assertNotIn('SECRETCANARY', str(store.corroboration_funnel(self.conn)))
