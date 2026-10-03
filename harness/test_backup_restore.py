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