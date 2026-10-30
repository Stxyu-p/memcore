"""
MemCore engine unit tests ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â Phase 1.

Tests the real core operations the harness evaluations don't cover:
idempotency replay, promote permissions, deactivate/restore, search ranking,
FTS trigger sync, tombstone-guarded creation, migration bookkeeping.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from memcore import store, core


class CoreTestBase(unittest.TestCase):
    """Temp file store + two agents (alice owner, bob member) in one project."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_unit_')
        self.db_path = os.path.join(self.tmpdir, 'unit.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-alpha'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'alpha')", (self.project,)
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
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class TestStore(CoreTestBase):

    def test_migrations_recorded(self):
        rows = self.conn.execute(
            'SELECT version FROM schema_migrations ORDER BY version'
        ).fetchall()
        versions = [r[0] for r in rows]
        # Contract: every declared migration is recorded, in declared order.
        expected = [name for name, _ in store.MIGRATIONS]
        self.assertEqual(versions, expected)

    def test_reopen_does_not_reapply_migrations(self):
        self.conn.close()
        conn2 = store.open_store(self.db_path)
        n = conn2.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0]
        conn2.close()
        self.assertEqual(n, len(store.MIGRATIONS))

    def test_successful_boot_leaves_no_migration_lock(self):
        self.assertEqual(store.check_migration_lock(self.conn), [])
        row = self.conn.execute(
            'SELECT 1 FROM schema_migrations WHERE version=?',
            (store._MIGRATION_LOCK_VERSION,)
        ).fetchone()
        self.assertIsNone(row)

    def test_expired_migration_lock_is_reclaimed_on_open(self):
        self.conn.execute(
            'INSERT INTO schema_migrations '
            '(version, applied_at, lock_holder, lock_until) '
            "VALUES (?, datetime('now'), 'crashed-worker', 0)",
            (store._MIGRATION_LOCK_VERSION,)
        )
        self.conn.close()
        conn2 = store.open_store(self.db_path)
        try:
            self.assertEqual(store.check_migration_lock(conn2), [])
        finally:
            conn2.close()

    def test_doctor_lock_check_reports_stale_lock_rows(self):
        self.conn.execute(
            'INSERT INTO schema_migrations '
            '(version, applied_at, lock_holder, lock_until) '
            "VALUES (?, datetime('now'), 'crashed-worker', 0)",
            (store._MIGRATION_LOCK_VERSION,)
        )
        locks = store.check_migration_lock(self.conn)
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0][1], 'crashed-worker')

    def test_migration_renew_fails_if_lease_was_reclaimed(self):
        holder = store._acquire_migration_lock(self.conn)
        self.conn.execute(
            'UPDATE schema_migrations SET lock_holder=?, lock_until=? '
            'WHERE version=?',
            ('other-worker', 9999999999.0, store._MIGRATION_LOCK_VERSION)
        )
        with self.assertRaises(store.StoreError):
            store._renew_migration_lock(self.conn, holder)
        store._release_migration_lock(self.conn, holder)
        row = self.conn.execute(
            'SELECT lock_holder FROM schema_migrations WHERE version=?',
            (store._MIGRATION_LOCK_VERSION,)
        ).fetchone()
        self.assertEqual(row[0], 'other-worker')
        self.conn.execute(
            'DELETE FROM schema_migrations WHERE version=?',
            (store._MIGRATION_LOCK_VERSION,)
        )

    def test_concurrent_fresh_boots_do_not_interleave_migrations(self):
        fresh = os.path.join(self.tmpdir, 'concurrent.db')
        barrier = threading.Barrier(3)
        errors = []
        versions = []

        def worker():
            try:
                barrier.wait()
                conn = store.open_store(fresh)
                try:
                    versions.append(conn.execute(
                        'SELECT COUNT(*) FROM schema_migrations WHERE version != ?',
                        (store._MIGRATION_LOCK_VERSION,)
                    ).fetchone()[0])
                finally:
                    conn.close()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(versions, [len(store.MIGRATIONS)] * 2)
        check = store.open_store_readonly(fresh)
        try:
            self.assertEqual(store.check_migration_lock(check), [])
        finally:
            check.close()

    def test_readonly_store_rejects_writes(self):
        ro = store.open_store_readonly(self.db_path)
        try:
            self.assertEqual(ro.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("INSERT INTO project (id, name) VALUES ('proj-nope', 'nope')")
        finally:
            ro.close()

    def test_readonly_store_does_not_create_missing_db(self):
        missing = os.path.join(self.tmpdir, 'missing.db')
        with self.assertRaises(store.StoreError):
            store.open_store_readonly(missing)
        self.assertFalse(os.path.exists(missing))

    def test_integrity_migration_enforces_unique_project_names(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO project (id,name) VALUES ('proj-duplicate-alpha','alpha')"
            )

    def test_integrity_migration_aborts_malformed_version_timestamp(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO memory_version "
                "(id,memory_id,content,created_by_agent_id,created_at) "
                "VALUES ('ver-bad-ts','mem-does-not-matter','bad timestamp','agent-alice','not-a-time')"
            )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM memory_version WHERE id='ver-bad-ts'"
        ).fetchone())

    def test_unknown_future_schema_version_fails_closed(self):
        self.conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES ('9999_future', '2999-01-01 00:00:00')"
        )
        self.conn.commit()
        self.conn.close()
        with self.assertRaises(store.StoreError):
            store.open_store(self.db_path)

    def test_failed_open_releases_database_handle(self):
        self.conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES ('9999_future', '2999-01-01 00:00:00')"
        )
        self.conn.close()
        with self.assertRaises(store.StoreError):
            store.open_store(self.db_path)
        moved = self.db_path + '.moved'
        os.replace(self.db_path, moved)
        self.assertTrue(os.path.exists(moved))
        os.replace(moved, self.db_path)

    def test_upgrade_from_pre_0004_db(self):
        """A DB created before the schema revision upgrades in place, data intact."""
        self.conn.close()
        # Start from a genuinely old store. Reusing setUp's already-0006 DB
        # would correctly fail closed when loaded by a simulated 0003 engine.
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass
        real = store.MIGRATIONS
        store.MIGRATIONS = real[:3]  # simulate the old engine (0001ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦ÃƒÂ¢Ã¢â€šÂ¬Ã…â€œ0003 only)
        try:
            old = store.open_store(self.db_path)
            old.execute("INSERT INTO project (id, name) VALUES ('proj-old', 'Legacy')")
            old.execute(
                "INSERT INTO agent (id, name, profile_key) VALUES ('agent-a', 'A', 'a')"
            )
            old.execute(
                "INSERT INTO memory (id, project_id, scope, owner_agent_id) "
                "VALUES ('mem-old', 'proj-old', 'project', 'agent-a')"
            )
            old.execute(
                "INSERT INTO memory_version (id, memory_id, content, "
                "  created_by_agent_id) VALUES ('ver-old', 'mem-old', 'legacy fact', 'agent-a')"
            )
            old.execute(
                "UPDATE memory SET current_version_id='ver-old' WHERE id='mem-old'"
            )
            old.close()
        finally:
            store.MIGRATIONS = real
        conn = store.open_store(self.db_path)  # new engine upgrades to 0005
        self.assertEqual(
            conn.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0],
            len(store.MIGRATIONS),
        )
        self.assertEqual(
            conn.execute(
                'SELECT content FROM memory_version WHERE id=?', ('ver-old',)
            ).fetchone()[0],
            'legacy fact',
        )
        # FTS was rebuilt in step with the migrated rows.
        hits = conn.execute(
            "SELECT rowid FROM memory_version_fts WHERE memory_version_fts MATCH 'legacy'"
        ).fetchall()
        self.assertEqual(len(hits), 1)
        conn.close()

    def test_failed_fresh_bootstrap_rolls_back_schema_and_bookkeeping(self):
        self.conn.close()
        bad_schema = os.path.join(self.tmpdir, 'bad_schema.sql')
        with open(bad_schema, 'w', encoding='utf-8') as f:
            f.write('CREATE TABLE bootstrap_partial (id INTEGER);\n'
                    'INSERT INTO definitely_missing_table VALUES (1);\n')
        fresh = os.path.join(self.tmpdir, 'fresh_fail.db')
        raw = sqlite3.connect(fresh, isolation_level=None)
        raw.execute('CREATE TABLE schema_migrations ('
                    'version TEXT PRIMARY KEY, applied_at TEXT, lock_holder TEXT, lock_until REAL)')
        original_schema = store.SCHEMA_PATH
        store.SCHEMA_PATH = original_schema.__class__(bad_schema)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store._apply_migration(raw, '0001_initial_contract', None)
        finally:
            store.SCHEMA_PATH = original_schema
        self.assertIsNone(raw.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bootstrap_partial'"
        ).fetchone())
        self.assertIsNone(raw.execute(
            "SELECT 1 FROM schema_migrations WHERE version='0001_initial_contract'"
        ).fetchone())
        raw.close()

    def test_failed_migration_rolls_back_schema_and_bookkeeping(self):
        """A failed future migration leaves neither DDL nor migration record."""
        self.conn.close()
        real = store.MIGRATIONS
        bad_name = '9998_test_failure'
        bad_sql = (
            'CREATE TABLE migration_should_rollback (id INTEGER);\n'
            'INSERT INTO definitely_missing_table VALUES (1);\n'
        )
        store.MIGRATIONS = real + [(bad_name, bad_sql)]
        try:
            with self.assertRaises(sqlite3.OperationalError):
                store.open_store(self.db_path)
        finally:
            store.MIGRATIONS = real
        raw = sqlite3.connect(self.db_path)
        try:
            table = raw.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='migration_should_rollback'"
            ).fetchone()
            recorded = raw.execute(
                'SELECT 1 FROM schema_migrations WHERE version=?', (bad_name,)
            ).fetchone()
            self.assertIsNone(table)
            self.assertIsNone(recorded)
        finally:
            raw.close()


class TestCreateMemory(CoreTestBase):

    def test_create_private_memory(self):
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'Alice private scratch note'
        )
        row = self.conn.execute(
            'SELECT scope, lifecycle, owner_agent_id FROM memory WHERE id=?',
            (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'private')
        self.assertEqual(row[1], 'candidate')
        self.assertEqual(row[2], self.alice)

    def test_engine_owned_create_timestamps_are_iso_z(self):
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'timestamp contract claim',
            scope='project', idempotency_key='timestamp-contract-key'
        )
        mem = self.conn.execute(
            'SELECT created_at,updated_at FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        ver = self.conn.execute(
            'SELECT created_at,valid_from FROM memory_version WHERE id=?', (ver_id,)
        ).fetchone()
        audit = self.conn.execute(
            "SELECT created_at FROM audit_event WHERE memory_id=? AND action='create'", (mem_id,)
        ).fetchone()[0]
        idem = self.conn.execute(
            'SELECT created_at FROM idempotency_key WHERE key=?', ('timestamp-contract-key',)
        ).fetchone()[0]
        for value in (*mem, *ver, audit, idem):
            self.assertTrue(value.endswith('Z'), value)
        self.assertEqual(mem[0], mem[1])
        self.assertEqual(ver[0], ver[1])

    def test_create_project_memory_requires_membership(self):
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        with self.assertRaises(core.PermissionDenied):
            core.create_memory(
                self.conn, self.project, 'agent-mallory', 'injected claim',
                scope='project'
            )

    def test_create_private_memory_requires_membership(self):
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        with self.assertRaises(core.PermissionDenied):
            core.create_memory(
                self.conn, self.project, 'agent-mallory', 'private bypass attempt',
                scope='private'
            )

    def test_create_rejects_empty_content_and_terminal_initial_lifecycle(self):
        for bad_content in ('', '   ', None):
            with self.subTest(content=bad_content), self.assertRaises(core.MemCoreError):
                core.create_memory(
                    self.conn, self.project, self.alice, bad_content, scope='project'
                )
        for terminal in ('rejected', 'disabled', 'superseded'):
            with self.subTest(lifecycle=terminal), self.assertRaises(core.MemCoreError):
                core.create_memory(
                    self.conn, self.project, self.alice, 'terminal direct create',
                    scope='project', lifecycle=terminal
                )

    def test_idempotency_key_replays_same_ids(self):
        r1 = core.create_memory(
            self.conn, self.project, self.alice, 'shared decision',
            scope='project', idempotency_key='key-1'
        )
        r2 = core.create_memory(
            self.conn, self.project, self.alice, 'shared decision',
            scope='project', idempotency_key='key-1'
        )
        self.assertEqual(r1, r2)
        n = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='create'"
        ).fetchone()[0]
        self.assertEqual(n, 1, 'replayed create must not write a second audit row')

    def test_idempotency_key_cannot_cross_projects(self):
        core.create_memory(
            self.conn, self.project, self.alice, 'project a claim',
            scope='project', idempotency_key='shared-key'
        )
        self.conn.execute("INSERT INTO project (id,name) VALUES ('proj-b','b')")
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-b',?,'member')", (self.alice,)
        )
        with self.assertRaises(core.PermissionDenied):
            core.create_memory(
                self.conn, 'proj-b', self.alice, 'project b claim',
                scope='project', idempotency_key='shared-key'
            )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM memory WHERE project_id='proj-b'").fetchone()[0],
            0
        )

    def test_idempotency_key_rejects_payload_mismatch(self):
        core.create_memory(
            self.conn, self.project, self.alice, 'first payload',
            scope='project', idempotency_key='payload-key'
        )
        with self.assertRaises(core.MemCoreError):
            core.create_memory(
                self.conn, self.project, self.alice, 'different payload',
                scope='project', idempotency_key='payload-key'
            )

    def test_rejected_idempotent_replay_still_honors_tombstone(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'later rejected idempotent claim',
            scope='project', idempotency_key='reject-key'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice, 'later rejected idempotent claim',
                scope='project', idempotency_key='reject-key'
            )

    def test_disabled_idempotent_replay_does_not_return_stale_success(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'later disabled idempotent claim',
            scope='project', idempotency_key='disabled-key'
        )
        core.deactivate(self.conn, mem_id, self.alice, reason='operator disabled')
        with self.assertRaises(core.MemCoreError):
            core.create_memory(
                self.conn, self.project, self.alice, 'later disabled idempotent claim',
                scope='project', idempotency_key='disabled-key'
            )
        self.assertEqual(
            self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()[0],
            'disabled'
        )

    def test_corrected_idempotent_replay_honors_old_claim_tombstone(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'old idempotent claim',
            scope='project', idempotency_key='corrected-key'
        )
        core.supersede(
            self.conn, mem_id, self.alice, 'new corrected claim', reason='fix old claim'
        )
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice, 'old idempotent claim',
                scope='project', idempotency_key='corrected-key'
            )

    def test_nonmember_cannot_probe_tombstone_state(self):
        claim = 'private project rejected fact'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, claim, scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('agent-mallory','mallory','mallory')"
        )
        with self.assertRaises(core.PermissionDenied):
            core.create_memory(
                self.conn, self.project, 'agent-mallory', claim, scope='project'
            )

    def test_private_rejection_is_owner_scoped_and_does_not_block_project(self):
        claim = 'alice private rejected observation'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, claim, scope='private'
        )
        core.reject(self.conn, mem_id, self.alice, 'private correction')
        scope = self.conn.execute(
            'SELECT scope FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint(claim),)
        ).fetchone()[0]
        self.assertEqual(scope, core._private_tombstone_scope(self.project, self.alice))
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice, claim, scope='private'
            )
        # A private refusal must neither leak into nor block another member's lane.
        core.create_memory(self.conn, self.project, self.bob, claim, scope='private')
        core.create_memory(self.conn, self.project, self.bob, claim, scope='project')

    def test_project_rejection_blocks_private_recapture_in_same_project(self):
        claim = 'project-wide known bad claim'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, claim, scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'project rejection')
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob, claim, scope='private'
            )

    def test_tombstone_blocks_identical_claim(self):
        claim = 'Alpha uses PostgreSQL for storage.'
        core.create_memory(
            self.conn, self.project, self.alice, claim, scope='project'
        )
        mem_id = self.conn.execute(
            'SELECT id FROM memory ORDER BY created_at DESC LIMIT 1'
        ).fetchone()[0]
        core.reject(self.conn, mem_id, self.alice, 'wrong storage engine')
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.bob, claim, scope='project'
            )

    def test_tombstone_blocks_whitespace_case_variant(self):
        claim = 'Alpha uses PostgreSQL for storage.'
        core.create_memory(
            self.conn, self.project, self.alice, claim, scope='project'
        )
        mem_id = self.conn.execute(
            'SELECT id FROM memory ORDER BY created_at DESC LIMIT 1'
        ).fetchone()[0]
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        variant = '  ALPHA   uses postgresql\nfor storage.  '
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(self.conn, self.project, self.bob, variant)


class TestPromote(CoreTestBase):

    def test_owner_promotes_own_private_memory(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'promotable insight'
        )
        core.promote(self.conn, mem_id, self.alice)
        row = self.conn.execute(
            'SELECT scope FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'project')

    def test_non_owner_member_cannot_promote_anothers_memory(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'alice private'
        )
        with self.assertRaises(core.PermissionDenied):
            core.promote(self.conn, mem_id, self.bob)

    def test_promotion_is_audited(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'audited promotion'
        )
        core.promote(self.conn, mem_id, self.alice)
        row = self.conn.execute(
            "SELECT action, actor_agent_id FROM audit_event "
            'WHERE memory_id=? ORDER BY id DESC LIMIT 1',
            (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'promote')
        self.assertEqual(row[1], self.alice)

    def test_promotion_blocked_if_claim_was_tombstoned_after_private_create(self):
        content = 'private claim later proven wrong'
        private_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='private'
        )
        duplicate_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        core.reject(self.conn, duplicate_id, self.alice, 'wrong')
        with self.assertRaises(core.TombstoneBlocked):
            core.promote(self.conn, private_id, self.alice)
        scope = self.conn.execute(
            'SELECT scope FROM memory WHERE id=?', (private_id,)
        ).fetchone()[0]
        self.assertEqual(scope, 'private')

    def test_rejected_private_memory_cannot_be_promoted(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'rejected private candidate', scope='private'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        with self.assertRaises(core.MemCoreError):
            core.promote(self.conn, mem_id, self.alice)


class TestSupersedeAndDeactivate(CoreTestBase):

    def test_supersede_keeps_history_and_flips_current(self):
        mem_id, v1 = core.create_memory(
            self.conn, self.project, self.alice, 'original decision', scope='project'
        )
        v2 = core.supersede(
            self.conn, mem_id, self.bob, 'corrected decision', reason='wrong info'
        )
        cur = self.conn.execute(
            'SELECT current_version_id FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(cur, v2)
        versions = core.superseded_history(self.conn, mem_id, self.alice)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0][0], v1)

    def test_supersede_resets_trust_and_closes_temporal_interval(self):
        mem_id, old_ver = core.create_memory(
            self.conn, self.project, self.alice, 'verified old claim', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET lifecycle='accepted', verification='source_backed', "
            "freshness='stale' WHERE id=?", (mem_id,)
        )
        ev_id = core._new_id('ev')
        self.conn.execute(
            "INSERT INTO evidence (id,kind,source_uri) VALUES (?, 'file', 'file:///old')",
            (ev_id,)
        )
        self.conn.execute(
            "INSERT INTO evidence_link (evidence_id,memory_version_id,relation) "
            "VALUES (?,?,'supports')", (ev_id, old_ver)
        )
        new_ver = core.supersede(
            self.conn, mem_id, self.alice, 'new unverified claim', reason='correction'
        )
        lifecycle, verification, freshness = self.conn.execute(
            'SELECT lifecycle,verification,freshness FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual((lifecycle, verification, freshness),
                         ('candidate', 'unverified', 'current'))
        old_until = self.conn.execute(
            'SELECT valid_until FROM memory_version WHERE id=?', (old_ver,)
        ).fetchone()[0]
        new_from = self.conn.execute(
            'SELECT valid_from FROM memory_version WHERE id=?', (new_ver,)
        ).fetchone()[0]
        self.assertIsNotNone(old_until)
        self.assertEqual(old_until, new_from)
        self.assertEqual(
            self.conn.execute(
                'SELECT COUNT(*) FROM evidence_link WHERE memory_version_id=?', (new_ver,)
            ).fetchone()[0],
            0
        )

    def test_supersede_tombstones_old_claim_against_resurrection(self):
        old_content = 'REST endpoint is /v1/old'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, old_content, scope='project'
        )
        core.supersede(
            self.conn, mem_id, self.alice, 'REST endpoint is /v2/new', reason='API correction'
        )
        fp = core.fingerprint(old_content)
        self.assertIsNotNone(
            self.conn.execute(
                'SELECT 1 FROM tombstone WHERE claim_fingerprint=? AND scope=? '
                'AND overridden_by IS NULL', (fp, self.project)
            ).fetchone()
        )
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice, old_content, scope='project'
            )

    def test_terminal_memories_cannot_be_superseded(self):
        for terminal in ('disabled', 'rejected'):
            with self.subTest(terminal=terminal):
                content = f'terminal correction guard {terminal}'
                mem_id, _ = core.create_memory(
                    self.conn, self.project, self.alice, content, scope='project'
                )
                if terminal == 'disabled':
                    core.deactivate(self.conn, mem_id, self.alice)
                else:
                    core.reject(self.conn, mem_id, self.alice, 'wrong')
                with self.assertRaises(core.MemCoreError):
                    core.supersede(self.conn, mem_id, self.alice, 'should not reactivate')

    def test_supersede_rejects_equivalent_claim_without_changing_trust(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'Same Claim',
            scope='project', lifecycle='accepted'
        )
        before_versions = self.conn.execute(
            'SELECT COUNT(*) FROM memory_version WHERE memory_id=?', (mem_id,)
        ).fetchone()[0]
        with self.assertRaises(core.MemCoreError):
            core.supersede(self.conn, mem_id, self.alice, '  same   claim  ')
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        after_versions = self.conn.execute(
            'SELECT COUNT(*) FROM memory_version WHERE memory_id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'accepted')
        self.assertEqual(after_versions, before_versions)

    def test_reject_cannot_create_unguarded_rejected_state(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'must stay guarded', scope='project'
        )
        with self.assertRaises(core.MemCoreError):
            core.reject(self.conn, mem_id, self.alice, 'wrong', create_tombstone=False)
        self.assertEqual(
            self.conn.execute('SELECT lifecycle FROM memory WHERE id=?', (mem_id,)).fetchone()[0],
            'candidate'
        )

    def test_project_tombstone_override_requires_owner_and_reopens_admission(self):
        content = 'project guard override claim'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong project claim')
        tomb_id = self.conn.execute(
            'SELECT id FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint(content),)
        ).fetchone()[0]
        with self.assertRaises(core.PermissionDenied):
            core.override_tombstone(self.conn, tomb_id, self.bob)
        self.assertTrue(core.override_tombstone(self.conn, tomb_id, self.alice))
        self.assertFalse(core.override_tombstone(self.conn, tomb_id, self.alice))
        self.assertTrue(core.admission_allowed(
            self.conn, content, self.project, scope='project', agent_id=self.alice
        ))
        self.assertEqual(
            self.conn.execute('SELECT lifecycle FROM memory WHERE id=?', (mem_id,)).fetchone()[0],
            'rejected'
        )
        audit = self.conn.execute(
            "SELECT action FROM audit_event WHERE action='tombstone_override' "
            'AND project_id=?', (self.project,)
        ).fetchone()
        self.assertIsNotNone(audit)

    def test_private_tombstone_override_is_owner_scoped(self):
        content = 'alice private guard override claim'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='private'
        )
        core.reject(self.conn, mem_id, self.alice, 'private wrong')
        tomb_id = self.conn.execute(
            'SELECT id FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint(content),)
        ).fetchone()[0]
        with self.assertRaises(core.PermissionDenied):
            core.override_tombstone(self.conn, tomb_id, self.bob)
        self.assertTrue(core.override_tombstone(self.conn, tomb_id, self.alice))
        self.assertTrue(core.admission_allowed(
            self.conn, content, self.project, scope='private', agent_id=self.alice
        ))

    def test_global_tombstone_override_fails_closed_without_admin_model(self):
        tomb_id = 'tomb-global-override-test'
        self.conn.execute(
            "INSERT INTO tombstone (id,claim_fingerprint,scope,reason) "
            "VALUES (?,? ,'global','global bad claim')",
            (tomb_id, core.fingerprint('global bad claim'))
        )
        with self.assertRaises(core.PermissionDenied):
            core.override_tombstone(self.conn, tomb_id, self.alice)

    def test_reject_repairs_missing_tombstone_on_legacy_rejected_memory(self):
        content = 'legacy rejected row missing its guard'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET lifecycle='rejected' WHERE id=?", (mem_id,)
        )
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM tombstone').fetchone()[0], 0)
        self.assertFalse(core.reject(self.conn, mem_id, self.alice, 'repair guard'))
        self.assertEqual(
            self.conn.execute(
                'SELECT COUNT(*) FROM tombstone WHERE claim_fingerprint=? AND scope=?',
                (core.fingerprint(content), self.project)
            ).fetchone()[0],
            1
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT action FROM audit_event WHERE memory_id=? ORDER BY id DESC LIMIT 1",
                (mem_id,)
            ).fetchone()[0],
            'reject_tombstone_repair'
        )

    def test_nonmember_mutation_does_not_reveal_memory_existence(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'cross project existence secret', scope='private'
        )
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('agent-mallory','mallory','mallory')"
        )
        for target in (mem_id, 'mem-definitely-missing'):
            with self.subTest(target=target), self.assertRaises(core.PermissionDenied):
                core._require_memory_write_access(self.conn, target, 'agent-mallory')
            with self.subTest(promote=target), self.assertRaises(core.PermissionDenied):
                core.promote(self.conn, target, 'agent-mallory')
            with self.subTest(supersede_wrapper=target), self.assertRaises(core.PermissionDenied):
                core.supersede_memory(
                    self.conn, target, 'agent-mallory', 'rewrite',
                    new_project_id=self.project
                )

    def test_private_history_does_not_leak_to_other_member(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'alice private history', scope='private'
        )
        core.supersede(self.conn, mem_id, self.alice, 'alice private history updated')
        self.assertEqual(core.superseded_history(self.conn, mem_id, self.bob), [])
        self.assertEqual(len(core.superseded_history(self.conn, mem_id, self.alice)), 2)

    def test_supersede_audited(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'to correct', scope='project'
        )
        core.supersede(self.conn, mem_id, self.alice, 'corrected', reason='fix')
        action = self.conn.execute(
            "SELECT action FROM audit_event WHERE memory_id=? "
            'ORDER BY id DESC LIMIT 1',
            (mem_id,)
        ).fetchone()[0]
        self.assertEqual(action, 'supersede')

    def test_supersede_memory_rejects_silent_cross_project_move(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'stay in project', scope='project'
        )
        with self.assertRaises(core.MemCoreError):
            core.supersede_memory(
                self.conn, mem_id, self.alice, 'should not move',
                new_project_id='proj-other'
            )
        project_id = self.conn.execute(
            'SELECT project_id FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(project_id, self.project)

    def test_member_cannot_modify_anothers_private_memory(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'alice private mutable note', scope='private'
        )
        with self.assertRaises(core.PermissionDenied):
            core.supersede(self.conn, mem_id, self.bob, 'bob rewrite')
        with self.assertRaises(core.PermissionDenied):
            core.reject(self.conn, mem_id, self.bob, 'bob reject')
        with self.assertRaises(core.PermissionDenied):
            core.deactivate(self.conn, mem_id, self.bob)

    def test_rejected_memory_cannot_be_deactivated_and_resurrected(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'bad terminal claim', scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        with self.assertRaises(core.MemCoreError):
            core.deactivate(self.conn, mem_id, self.alice)
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'rejected')

    def test_reject_is_idempotent_and_does_not_duplicate_tombstones(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'reject only once', scope='project'
        )
        self.assertTrue(core.reject(self.conn, mem_id, self.alice, 'wrong'))
        self.assertFalse(core.reject(self.conn, mem_id, self.alice, 'wrong again'))
        fp = core.fingerprint('reject only once')
        n = self.conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE claim_fingerprint=? AND scope=? '
            'AND overridden_by IS NULL',
            (fp, self.project)
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_two_duplicate_memories_share_one_active_tombstone(self):
        content = 'duplicate claim existed before rejection'
        first, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        second, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        core.reject(self.conn, first, self.alice, 'wrong')
        core.reject(self.conn, second, self.alice, 'also wrong')
        fp = core.fingerprint(content)
        n = self.conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE claim_fingerprint=? AND scope=? '
            'AND overridden_by IS NULL',
            (fp, self.project)
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_restore_is_blocked_by_active_tombstone(self):
        content = 'claim disabled before later rejection'
        original, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        core.deactivate(self.conn, original, self.alice)
        duplicate, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        core.reject(self.conn, duplicate, self.alice, 'later found wrong')
        with self.assertRaises(core.TombstoneBlocked):
            core.restore(self.conn, original, self.alice)

    def test_deactivate_restore_roundtrip(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'temporary note'
        )
        core.deactivate(self.conn, mem_id, self.alice, reason='cleanup')
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'disabled')
        # Disabled memories are excluded from visible recall
        visible = core.visible_memories(self.conn, self.project, self.alice)
        self.assertNotIn(mem_id, [r[0] for r in visible])
        core.restore(self.conn, mem_id, self.alice)
        row = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(row[0], 'candidate')
    def test_deactivate_restore_preserves_previous_lifecycle(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'accepted reversible memory', scope='project'
        )
        self.conn.execute("UPDATE memory SET lifecycle='accepted' WHERE id=?", (mem_id,))
        core.deactivate(self.conn, mem_id, self.alice, reason='temporary hide')
        detail = self.conn.execute(
            "SELECT detail FROM audit_event WHERE memory_id=? AND action='deactivate' ORDER BY id DESC LIMIT 1",
            (mem_id,)
        ).fetchone()[0]
        self.assertIn('accepted', detail)
        core.restore(self.conn, mem_id, self.alice)
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'accepted')

    def test_repeated_deactivate_cannot_overwrite_restore_state(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'double deactivate guard', scope='project'
        )
        self.conn.execute("UPDATE memory SET lifecycle='conflict' WHERE id=?", (mem_id,))
        core.deactivate(self.conn, mem_id, self.alice)
        with self.assertRaises(core.MemCoreError):
            core.deactivate(self.conn, mem_id, self.alice)
        core.restore(self.conn, mem_id, self.alice)
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'conflict')



class TestSearch(CoreTestBase):

    def test_fts_finds_and_respects_scope(self):
        core.create_memory(
            self.conn, self.project, self.alice,
            'The deploy pipeline uses GitHub Actions runners',
            scope='project'
        )
        core.create_memory(
            self.conn, self.project, self.alice,
            'Secret deploy pipeline token stored privately',
            scope='private'
        )
        # bob searches: sees project memory, never alice's private one
        hits = core.search(self.conn, self.project, self.bob, 'deploy pipeline')
        contents = [r[5] for r in hits]
        self.assertEqual(len(contents), 1)
        self.assertIn('GitHub Actions', contents[0])

    def test_visible_memories_include_rejected_flag_is_honored(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice,
            'rejected but inspectable claim', scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        hidden = core.visible_memories(self.conn, self.project, self.alice)
        shown = core.visible_memories(
            self.conn, self.project, self.alice, include_rejected=True
        )
        self.assertNotIn(mem_id, [r[0] for r in hidden])
        self.assertIn(mem_id, [r[0] for r in shown])

    def test_nonmember_cannot_read_project_memory(self):
        core.create_memory(
            self.conn, self.project, self.alice,
            'shared deployment secretless fact', scope='project'
        )
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        self.assertEqual(
            core.search(self.conn, self.project, 'agent-mallory', 'deployment'), []
        )
        self.assertEqual(
            core.visible_memories(self.conn, self.project, 'agent-mallory'), []
        )

    def test_fts_trigger_keeps_index_in_sync(self):
        core.create_memory(
            self.conn, self.project, self.alice, 'unique xyzzy content', scope='project'
        )
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'second memory about quux', scope='project'
        )
        fts_rows = self.conn.execute('SELECT COUNT(*) FROM memory_version_fts').fetchone()[0]
        ver_rows = self.conn.execute('SELECT COUNT(*) FROM memory_version').fetchone()[0]
        self.assertEqual(fts_rows, ver_rows, 'FTS index out of sync after inserts')

        # UPDATE path: supersede rewrites content; FTS must follow
        core.supersede(self.conn, mem_id, self.alice, 'rewritten content about quux2')
        hits = core.search(self.conn, self.project, self.alice, 'quux2', query_vec=[])
        self.assertEqual(len(hits), 1, 'FTS did not pick up updated content')

    def test_search_supports_unicode_thai_query(self):
        core.create_memory(
            self.conn, self.project, self.alice,
            'ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â°ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â²ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂªÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â«ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â±ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¾ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â‚¬Å¾Ã‚Â¢', scope='project'
        )
        hits = core.search(self.conn, self.project, self.bob, 'ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â°ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â²ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡')
        self.assertEqual(len(hits), 1)
        self.assertIn('ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â°ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¦ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Â¦Ãƒâ€šÃ‚Â¾ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â²ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â£ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¹ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â¹ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â§ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¸ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡', hits[0][5])

    def test_unicode_exact_fast_path_supplements_casefolded_fts_matches(self):
        thai = '\u0e23\u0e30\u0e1a\u0e1a'
        first, _ = core.create_memory(
            self.conn, self.project, self.alice,
            f'{thai} Alpha deployment', scope='project', lifecycle='accepted'
        )
        second, _ = core.create_memory(
            self.conn, self.project, self.alice,
            f'{thai} alpha deployment backup', scope='project', lifecycle='accepted'
        )
        hits = core.search(self.conn, self.project, self.bob, f'{thai} Alpha')
        self.assertEqual({row[0] for row in hits}, {first, second})
        self.assertEqual(len(hits), 2)

    def test_search_survives_fts_operators_in_query(self):
        """Regression: raw user strings with FTS5 operator chars must not raise.

        MIKA reproduced: search "bob's CLI taste" crashed with
        sqlite3.OperationalError: fts5: syntax error near "'".
        """
        core.create_memory(
            self.conn, self.project, self.alice,
            "bob's CLI taste is questionable", scope='project'
        )
        # apostrophe, quotes, parens, hyphen ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â all previously fatal
        hits = core.search(self.conn, self.project, self.bob, "bob's CLI taste")
        contents = [r[5] for r in hits]
        self.assertEqual(len(contents), 1)
        self.assertIn('CLI taste', contents[0])
        # punctuation-only query: no crash, no results
        self.assertEqual(core.search(self.conn, self.project, self.bob, '!!! ???'), [])

    def test_search_returns_only_current_version(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'old claim about deploy', scope='project'
        )
        core.supersede(
            self.conn, mem_id, self.alice, 'new claim about deploy', reason='update'
        )
        rows = core.search(self.conn, self.project, self.alice, 'deploy')
        contents = [r[5] for r in rows if r[0] == mem_id]
        # current truth only ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â superseded content must not surface in retrieval
        self.assertEqual(contents, ['new claim about deploy'])

    def test_rank_prefers_pinned(self):
        mem_a, _ = core.create_memory(
            self.conn, self.project, self.alice, 'kubernetes deployment notes alpha',
            scope='project'
        )
        mem_b, _ = core.create_memory(
            self.conn, self.project, self.alice, 'kubernetes deployment notes beta',
            scope='project'
        )
        self.conn.execute('UPDATE memory SET pinned=1 WHERE id=?', (mem_b,))
        self.conn.commit()
        hits = core.search(self.conn, self.project, self.alice, 'kubernetes deployment')
        self.assertEqual(hits[0][0], mem_b, 'pinned memory must rank first')

    def test_search_rank_prefers_trusted_current_memory_on_equal_text(self):
        rows = []
        for lifecycle, verification, freshness in [
            ('candidate','unverified','current'),
            ('accepted','unverified','stale'),
            ('accepted','source_backed','current'),
            ('accepted','user_authoritative','current'),
        ]:
            mem_id, _ = core.create_memory(
                self.conn, self.project, self.alice,
                'equal ranking phrase for trust ordering', scope='project', lifecycle=lifecycle
            )
            self.conn.execute(
                'UPDATE memory SET verification=?, freshness=? WHERE id=?',
                (verification, freshness, mem_id)
            )
            rows.append((mem_id, lifecycle, verification, freshness))
        hits = core.search(self.conn, self.project, self.alice, 'equal ranking phrase')
        self.assertEqual(hits[0][0], rows[3][0])
        self.assertEqual(hits[1][0], rows[2][0])
        self.assertEqual(hits[-1][0], rows[0][0])

    def test_search_rejects_nonpositive_or_invalid_limits(self):
        for bad in (-1, 0, 'nope'):
            with self.subTest(limit=bad), self.assertRaises(core.MemCoreError):
                core.search(self.conn, self.project, self.alice, 'anything', limit=bad)


# ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ operational tooling tests ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚ÂÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬

class TestGC(CoreTestBase):
    """Tests for gc_scan (dry-run) and gc_apply (retention sweep)."""

    def test_gc_scan_finds_old_unlinked_candidates(self):
        """candidate memory older than cutoff with no evidence is a gc candidate."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'old claim with no evidence', scope='project'
        )
        # Backdate last activity beyond the candidate-days cutoff.
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-40 days') WHERE id=?",
            (mem_id,)
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0][0], mem_id)
        self.assertEqual(len(tombstones), 0)

    def test_gc_scan_skips_memories_with_evidence(self):
        """A candidate memory that has an evidence_link is not gc-eligible."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'evidenced claim', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-40 days') WHERE id=?",
            (mem_id,)
        )
        ev_id = core._new_id('ev')
        self.conn.execute(
            'INSERT INTO evidence (id, kind, source_uri, source_label) VALUES (?, ?, ?, ?)',
            (ev_id, 'file', 'file:///repo/main.py', 'main.py')
        )
        self.conn.execute(
            'INSERT INTO evidence_link (evidence_id, memory_version_id, relation) VALUES (?, ?, ?)',
            (ev_id, ver_id, 'supports')
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(tombstones), 0)

    def test_gc_uses_current_version_evidence_only(self):
        mem_id, old_ver = core.create_memory(
            self.conn, self.project, self.alice, 'evidenced old claim', scope='project'
        )
        ev_id = core._new_id('ev')
        self.conn.execute(
            'INSERT INTO evidence (id,kind,source_uri,source_label) VALUES (?,?,?,?)',
            (ev_id, 'test', 'test://old-version', 'old version evidence')
        )
        self.conn.execute(
            "INSERT INTO evidence_link (evidence_id,memory_version_id,relation) "
            "VALUES (?,?,'supports')", (ev_id, old_ver)
        )
        core.supersede(
            self.conn, mem_id, self.alice, 'replacement claim without evidence',
            reason='claim changed'
        )
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now','-40 days') WHERE id=?",
            (mem_id,)
        )
        candidates, _ = core.gc_scan(self.conn, candidate_days=30)
        self.assertIn(mem_id, [row[0] for row in candidates])
        disabled, _ = core.gc_apply(self.conn, candidate_days=30)
        self.assertIn(mem_id, disabled)
        self.assertEqual(
            self.conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()[0],
            'disabled'
        )

    def test_gc_scan_finds_old_overridden_tombstones(self):
        """Only old overridden tombstones are gc candidates for purging."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'rejected claim for tombstone', scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'outdated')
        tomb_id = self.conn.execute('SELECT id FROM tombstone').fetchone()[0]
        self.conn.execute(
            "UPDATE tombstone SET overridden_by=?, "
            "created_at=datetime('now', '-100 days') WHERE id=?",
            (self.alice, tomb_id)
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(tombstones), 1)
        self.assertEqual(tombstones[0][0], tomb_id)

    def test_gc_scan_does_not_touch_anything(self):
        """gc_scan is a pure read ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€¦Ã‚Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã†â€™Ãƒâ€šÃ‚Â¢ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡Ãƒâ€šÃ‚Â¬ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â no data should change."""
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'young candidate', scope='project'
        )
        candidates, tombstones = core.gc_scan(self.conn)
        self.assertEqual(len(candidates), 0)
        self.assertEqual(len(tombstones), 0)
        # Memory still candidate.
        lc = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lc, 'candidate')

    def test_gc_apply_disables_old_candidates_without_tombstone(self):
        """Age-based retention is reversible and does not declare a claim false."""
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'gc me', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now', '-40 days') WHERE id=?",
            (mem_id,)
        )
        self.conn.commit()
        disabled, purged = core.gc_apply(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(disabled, [mem_id])
        self.assertEqual(purged, [])
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'disabled')
        self.assertEqual(
            self.conn.execute(
                'SELECT COUNT(*) FROM tombstone WHERE claim_fingerprint=?',
                (core.fingerprint('gc me'),)
            ).fetchone()[0],
            0
        )
        self.assertTrue(core.admission_allowed(self.conn, 'gc me', self.project))

    def test_gc_apply_purges_old_overridden_tombstones(self):
        """gc_apply removes only overridden tombstones past the cutoff."""
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'gone soon', scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'obsolete')
        tomb_id = self.conn.execute('SELECT id FROM tombstone').fetchone()[0]
        self.conn.execute(
            "UPDATE tombstone SET overridden_by=?, "
            "created_at=datetime('now', '-100 days') WHERE id=?",
            (self.alice, tomb_id)
        )
        self.conn.commit()
        tombstoned, purged = core.gc_apply(self.conn, candidate_days=30, tombstone_days=90)
        self.assertEqual(len(tombstoned), 0)
        self.assertEqual(len(purged), 1)
        self.assertEqual(purged[0], tomb_id)
        count = self.conn.execute(
            'SELECT COUNT(*) FROM tombstone WHERE id=?', (tomb_id,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_gc_never_purges_active_tombstone_by_age(self):
        content = 'known bad claim must stay blocked'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        core.reject(self.conn, mem_id, self.alice, 'wrong')
        tomb_id = self.conn.execute('SELECT id FROM tombstone').fetchone()[0]
        self.conn.execute(
            "UPDATE tombstone SET created_at=datetime('now', '-1000 days') WHERE id=?",
            (tomb_id,)
        )
        self.conn.commit()
        candidates, tombstones = core.gc_scan(
            self.conn, candidate_days=30, tombstone_days=90
        )
        self.assertEqual(tombstones, [])
        _, purged = core.gc_apply(
            self.conn, candidate_days=30, tombstone_days=90
        )
        self.assertEqual(purged, [])
        self.assertEqual(
            self.conn.execute(
                'SELECT COUNT(*) FROM tombstone WHERE id=?', (tomb_id,)
            ).fetchone()[0],
            1
        )
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(
                self.conn, self.project, self.alice, content, scope='project'
            )

    def test_gc_apply_does_not_touch_recent_memories(self):
        """gc_apply must not affect young candidates or recent tombstones."""
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'keep me', scope='project'
        )
        tombstoned, purged = core.gc_apply(self.conn)
        self.assertEqual(len(tombstoned), 0)
        self.assertEqual(len(purged), 0)
        lc = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lc, 'candidate')

    def test_gc_scan_parses_iso_z_timestamps(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'iso timestamp gc candidate', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now','-40 days') "
            'WHERE id=?', (mem_id,)
        )
        candidates, _ = core.gc_scan(self.conn, candidate_days=30)
        self.assertIn(mem_id, [row[0] for row in candidates])

    def test_gc_apply_rechecks_evidence_after_stale_scan(self):
        mem_id, ver_id = core.create_memory(
            self.conn, self.project, self.alice, 'race protected candidate', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET updated_at=datetime('now','-40 days') WHERE id=?", (mem_id,)
        )
        stale = core.gc_scan(self.conn, candidate_days=30, tombstone_days=90)
        ev_id = core._new_id('ev')
        self.conn.execute(
            'INSERT INTO evidence (id,kind,source_uri,source_label) VALUES (?,?,?,?)',
            (ev_id, 'test', 'test://late', 'late evidence')
        )
        self.conn.execute(
            "INSERT INTO evidence_link (evidence_id,memory_version_id,relation) VALUES (?,?,'supports')",
            (ev_id, ver_id)
        )
        original = core.gc_scan
        core.gc_scan = lambda *_a, **_k: stale
        try:
            tombstoned, _ = core.gc_apply(self.conn, candidate_days=30, tombstone_days=90)
        finally:
            core.gc_scan = original
        self.assertNotIn(mem_id, tombstoned)
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'candidate')

    def test_gc_skips_pinned_and_critical_candidates(self):
        pinned, _ = core.create_memory(
            self.conn, self.project, self.alice, 'pinned retention guard', scope='project'
        )
        critical, _ = core.create_memory(
            self.conn, self.project, self.alice, 'critical retention guard', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET pinned=1, updated_at=datetime('now','-100 days') WHERE id=?",
            (pinned,)
        )
        self.conn.execute(
            "UPDATE memory SET critical=1, updated_at=datetime('now','-100 days') WHERE id=?",
            (critical,)
        )
        candidates, _ = core.gc_scan(self.conn, candidate_days=30)
        ids = {row[0] for row in candidates}
        self.assertNotIn(pinned, ids)
        self.assertNotIn(critical, ids)
        disabled, _ = core.gc_apply(self.conn, candidate_days=30)
        self.assertNotIn(pinned, disabled)
        self.assertNotIn(critical, disabled)

    def test_recent_correction_of_old_memory_is_not_immediately_gc_eligible(self):
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, 'old claim before correction', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET created_at=datetime('now','-200 days'), "
            "updated_at=datetime('now','-200 days') WHERE id=?",
            (mem_id,)
        )
        core.supersede(
            self.conn, mem_id, self.alice, 'freshly corrected claim', reason='recent correction'
        )
        candidates, _ = core.gc_scan(self.conn, candidate_days=30)
        self.assertNotIn(mem_id, [row[0] for row in candidates])
        disabled, _ = core.gc_apply(self.conn, candidate_days=30)
        self.assertNotIn(mem_id, disabled)
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'candidate')


class TestStats(CoreTestBase):
    """Tests for core.stats()."""

    def setUp(self):
        super().setUp()
        core.create_memory(self.conn, self.project, self.alice,
                           'first project note here', scope='project')
        core.create_memory(self.conn, self.project, self.bob,
                           'second private note', scope='private')
        core.create_memory(self.conn, self.project, self.alice,
                           'third project note', scope='project')

    def test_stats_contains_lifecycle_counts(self):
        s = core.stats(self.conn)
        self.assertIn('by_lifecycle', s)
        self.assertGreaterEqual(s['memories_total'], 3)
        self.assertGreaterEqual(s['by_lifecycle'].get('candidate', 0), 3)

    def test_stats_contains_scope_counts(self):
        s = core.stats(self.conn)
        self.assertIn('by_scope', s)
        self.assertEqual(s['by_scope'].get('project', 0), 2)
        self.assertEqual(s['by_scope'].get('private', 0), 1)

    def test_stats_top_agents(self):
        s = core.stats(self.conn)
        self.assertTrue(s['top_agents'])
        top = s['top_agents'][0]
        self.assertEqual(top['agent'], 'agent-alice')
        self.assertEqual(top['memories'], 2)

    def test_stats_avg_summary_length(self):
        s = core.stats(self.conn)
        self.assertIn('avg_summary_length', s)
        self.assertGreater(s['avg_summary_length'], 0.0)

    def test_stats_fts_drift_check(self):
        s = core.stats(self.conn)
        self.assertIn('fts', s)
        fts_info = s['fts']
        self.assertEqual(fts_info['fts_rows'], fts_info['version_rows'])
        self.assertTrue(fts_info['in_sync'])


class TestImport(CoreTestBase):
    """Tests for core.import_memories()."""

    def setUp(self):
        super().setUp()
        self.items = [
            {'title': 'Item 1', 'summary': 'alpha deploy uses containers',
             'type': 'fact', 'evidence': [{'kind': 'file', 'source_uri': 'f1', 'source_label': 'l1'}]},
            {'title': 'Item 2', 'summary': 'beta deploy uses serverless',
             'type': 'observation'},
            {'title': 'Dup', 'summary': 'alpha deploy uses containers',
             'type': 'fact'},
            {'title': 'Empty', 'summary': '   ', 'type': 'fact'},
        ]

    def test_import_creates_and_skips_correctly(self):
        result = core.import_memories(
            self.conn, self.items, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 2)
        self.assertEqual(result['skipped'], 2)
        self.assertEqual(len(result['created']), 2)

    def test_import_via_create_requires_membership(self):
        """non-member agent cannot import project-scope memories."""
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-mallory', 'mallory', 'mallory')"
        )
        self.conn.commit()
        result = core.import_memories(
            self.conn, self.items, self.project, 'agent-mallory', scope='project'
        )
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped'], 4)

    def test_import_evidence_is_linked(self):
        """imported evidence rows are linked to the correct memory version."""
        result = core.import_memories(
            self.conn, self.items, self.project, self.alice, scope='project'
        )
        mem_id, ver_id = result['created'][0]
        links = self.conn.execute(
            'SELECT evidence_id FROM evidence_link WHERE memory_version_id=?',
            (ver_id,)
        ).fetchall()
        self.assertEqual(len(links), 1)
        ev = self.conn.execute(
            'SELECT kind FROM evidence WHERE id=?', (links[0][0],)
        ).fetchone()[0]
        self.assertEqual(ev, 'file')
        ev_ts = self.conn.execute('SELECT captured_at FROM evidence WHERE id=?', (links[0][0],)).fetchone()[0]
        link_ts = self.conn.execute('SELECT created_at FROM evidence_link WHERE evidence_id=? AND memory_version_id=?', (links[0][0], ver_id)).fetchone()[0]
        self.assertTrue(ev_ts.endswith('Z'), ev_ts)
        self.assertTrue(link_ts.endswith('Z'), link_ts)

    def test_import_rolls_back_memory_if_evidence_insert_fails(self):
        self.conn.execute(
            "INSERT INTO evidence (id, kind, source_uri) VALUES ('ev-collision', 'file', 'existing')"
        )
        self.conn.commit()
        item = [{'summary': 'atomic import claim', 'type': 'fact',
                 'evidence': [{'kind': 'file', 'source_uri': 'new'}]}]
        original_new_id = core._new_id
        core._new_id = lambda prefix: 'ev-collision' if prefix == 'ev' else original_new_id(prefix)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                core.import_memories(
                    self.conn, item, self.project, self.alice, scope='project'
                )
        finally:
            core._new_id = original_new_id
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM memory_version WHERE content='atomic import claim'"
            ).fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM audit_event WHERE detail LIKE '%atomic import claim%'"
            ).fetchone()[0], 0
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM idempotency_key WHERE key LIKE 'import:%'"
            ).fetchone()[0], 0
        )

    def test_import_dedup_by_fingerprint(self):
        """sha256 fingerprint dedup: same summary text -> one memory."""
        single = [{'summary': 'shared claim', 'type': 'fact'}]
        result = core.import_memories(
            self.conn, single, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['skipped'], 0)

    def test_plan_import_is_read_only_and_classifies_batch(self):
        before = self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0]
        plan = core.plan_import(self.conn, self.items, self.project)
        after = self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(plan['total'], 4)
        self.assertEqual(plan['would_add'], 2)
        self.assertEqual(plan['skipped'], 2)
        self.assertEqual(plan['reasons']['duplicate_input'], 1)
        self.assertEqual(plan['reasons']['empty_summary'], 1)

    def test_plan_import_detects_existing_import_without_writes(self):
        single = [{'summary': 'already imported claim', 'type': 'fact'}]
        core.import_memories(
            self.conn, single, self.project, self.alice, scope='project'
        )
        before = self.conn.total_changes
        plan = core.plan_import(self.conn, single, self.project)
        self.assertEqual(conn_changes := self.conn.total_changes, before)
        self.assertEqual(plan['would_add'], 0)
        self.assertEqual(plan['reasons']['already_imported'], 1)
        self.assertEqual(conn_changes, before)

    def test_import_skips_claim_already_present_from_other_write_path(self):
        content = 'manual memory already covers migration claim'
        core.create_memory(
            self.conn, self.project, self.alice, content, scope='project'
        )
        items = [{'summary': content, 'type': 'fact'}]
        plan = core.plan_import(self.conn, items, self.project)
        self.assertEqual(plan['would_add'], 0)
        self.assertEqual(plan['reasons']['already_present'], 1)
        result = core.import_memories(
            self.conn, items, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped'], 1)

    def test_private_claim_does_not_block_project_import(self):
        content = 'same words but different visibility contract'
        core.create_memory(
            self.conn, self.project, self.alice, content, scope='private'
        )
        items = [{'summary': content, 'type': 'fact'}]
        plan = core.plan_import(
            self.conn, items, self.project, scope='project', agent_id=self.alice
        )
        self.assertEqual(plan['would_add'], 1)
        result = core.import_memories(
            self.conn, items, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 1)
        scopes = self.conn.execute(
            'SELECT scope FROM memory m JOIN memory_version v '
            'ON v.id=m.current_version_id WHERE v.content=? ORDER BY scope',
            (content,)
        ).fetchall()
        self.assertEqual([row[0] for row in scopes], ['private', 'project'])

    def test_private_import_idempotency_is_scoped_per_agent_and_not_project(self):
        content = 'same import claim across independent visibility lanes'
        items = [{'summary': content, 'type': 'fact'}]
        first = core.import_memories(
            self.conn, items, self.project, self.alice, scope='private'
        )
        second = core.import_memories(
            self.conn, items, self.project, self.bob, scope='private'
        )
        shared = core.import_memories(
            self.conn, items, self.project, self.alice, scope='project'
        )
        self.assertEqual((first['added'], second['added'], shared['added']), (1, 1, 1))
        keys = [row[0] for row in self.conn.execute(
            "SELECT key FROM idempotency_key WHERE key LIKE 'import:%' ORDER BY key"
        )]
        self.assertEqual(len(keys), 3)
        self.assertTrue(any(':private:agent-alice:' in key for key in keys))
        self.assertTrue(any(':private:agent-bob:' in key for key in keys))

    def test_private_import_honors_owner_private_tombstone_only(self):
        content = 'private import rejected by alice only'
        mem_id, _ = core.create_memory(
            self.conn, self.project, self.alice, content, scope='private'
        )
        core.reject(self.conn, mem_id, self.alice, 'alice private rejection')
        items = [{'summary': content, 'type': 'fact'}]
        alice_plan = core.plan_import(
            self.conn, items, self.project, scope='private', agent_id=self.alice
        )
        bob_plan = core.plan_import(
            self.conn, items, self.project, scope='private', agent_id=self.bob
        )
        project_plan = core.plan_import(
            self.conn, items, self.project, scope='project', agent_id=self.bob
        )
        self.assertEqual(alice_plan['reasons']['tombstone_blocked'], 1)
        self.assertEqual(bob_plan['would_add'], 1)
        self.assertEqual(project_plan['would_add'], 1)

    def test_invalid_evidence_fields_match_dry_run_and_real_import(self):
        bad = [{
            'summary': 'malformed evidence binding',
            'evidence': [{'kind': 'file', 'source_uri': {'not': 'bindable'}}],
        }]
        plan = core.plan_import(self.conn, bad, self.project)
        self.assertEqual(plan['would_add'], 0)
        self.assertEqual(plan['reasons']['invalid_evidence'], 1)
        result = core.import_memories(
            self.conn, bad, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped'], 1)


    def test_invalid_evidence_kind_is_not_silently_rewritten(self):
        bad = [{
            'summary': 'typo evidence kind',
            'evidence': [{'kind': 'commti', 'source_uri': 'git://abc'}],
        }]
        plan = core.plan_import(self.conn, bad, self.project)
        self.assertEqual(plan['reasons']['invalid_evidence'], 1)
        result = core.import_memories(
            self.conn, bad, self.project, self.alice, scope='project'
        )
        self.assertEqual(result['added'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM evidence').fetchone()[0], 0)
    def test_legacy_source_evidence_is_preserved_in_metadata(self):
        items = [{'summary': 'legacy source evidence', 'evidence': [
            {'kind': 'source', 'source_uri': 'file:///legacy', 'source_label': 'legacy'}
        ]}]
        plan = core.plan_import(self.conn, items, self.project)
        self.assertEqual(plan['would_add'], 1)
        result = core.import_memories(self.conn, items, self.project, self.alice, scope='project')
        self.assertEqual(result['added'], 1)
        row = self.conn.execute(
            "SELECT kind, metadata FROM evidence WHERE source_uri='file:///legacy'"
        ).fetchone()
        self.assertEqual(row[0], 'external')
        self.assertIn('source', row[1])


if __name__ == '__main__':
    unittest.main()
