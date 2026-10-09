"""Deterministic performance regressions for MemCore hot paths.

These tests avoid wall-clock thresholds. They verify indexed query plans, runtime
openers that skip migration/WAL negotiation, and fingerprint lookups that do not
scan/fingerprint every private memory once migration 0010 is in place.
"""
import os
import sqlite3
import tempfile
import unicodedata
import unittest
from unittest import mock

from memcore import core, ingest, store


class PerformanceFastPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_perf_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.conn = store.open_store(self.db)
        self.conn.execute("INSERT INTO project (id,name) VALUES ('proj','demo')")
        self.conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('agent','alice','alice')"
        )
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj','agent','owner')"
        )

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_installs_fast_path_indexes(self):
        self.assertEqual(store.MIGRATIONS[-1][0], '0018_memory_embedding')
        memory_indexes = {
            row[1] for row in self.conn.execute("PRAGMA index_list('memory')").fetchall()
        }
        ingest_indexes = {
            row[1] for row in self.conn.execute("PRAGMA index_list('ingest_event')").fetchall()
        }
        derivation_indexes = {
            row[1] for row in self.conn.execute("PRAGMA index_list('ingest_derivation')").fetchall()
        }
        audit_indexes = {
            row[1] for row in self.conn.execute("PRAGMA index_list('audit_event')").fetchall()
        }
        self.assertIn('idx_memory_private_claim', memory_indexes)
        self.assertIn('idx_memory_project_claim', memory_indexes)
        self.assertIn('idx_ingest_event_pending_decision', ingest_indexes)
        self.assertIn('idx_ingest_derivation_memory_event', derivation_indexes)
        self.assertIn('idx_audit_mutation_recovery', audit_indexes)

    def test_upgrade_from_0009_backfills_current_fingerprints(self):
        legacy = os.path.join(self.tmp.name, 'legacy-0009.db')
        conn = sqlite3.connect(legacy, isolation_level=None)
        try:
            conn.execute('PRAGMA foreign_keys = ON')
            for stmt in store._script_statements(store.SCHEMA_PATH.read_text(encoding='utf-8')):
                conn.execute(stmt)
            conn.execute(
                'CREATE TABLE schema_migrations ('
                'version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime(\'now\')), '
                'lock_holder TEXT, lock_until REAL)'
            )
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES ('0001_initial_contract')"
            )
            for name, sql in store.MIGRATIONS[1:]:
                if name == '0010_performance_fast_paths':
                    break
                store._apply_migration(conn, name, sql)
            conn.execute("INSERT INTO project (id,name) VALUES ('p','legacy')")
            conn.execute(
                "INSERT INTO agent (id,name,profile_key) VALUES ('a','legacy','legacy')"
            )
            conn.execute(
                "INSERT INTO project_membership (project_id,agent_id,role) "
                "VALUES ('p','a','owner')"
            )
            conn.execute('BEGIN IMMEDIATE')
            conn.execute(
                "INSERT INTO memory "
                "(id,project_id,scope,owner_agent_id,current_version_id) "
                "VALUES ('m','p','private','a','v')"
            )
            conn.execute(
                "INSERT INTO memory_version "
                "(id,memory_id,content,created_by_agent_id) "
                "VALUES ('v','m','Legacy durable claim','a')"
            )
            conn.execute('COMMIT')
        finally:
            conn.close()

        upgraded = store.open_store(legacy)
        try:
            self.assertEqual(
                upgraded.execute('SELECT claim_fingerprint FROM memory WHERE id=\'m\'').fetchone()[0],
                core.fingerprint('Legacy durable claim')
            )
            self.assertEqual(store._current_version(upgraded), '0018_memory_embedding')
        finally:
            upgraded.close()

    def test_upgrade_from_0011_repairs_unicode_tombstone_and_idempotency(self):
        legacy = os.path.join(self.tmp.name, 'legacy-0011.db')
        conn = store.open_store(legacy)
        try:
            conn.execute("INSERT INTO project (id,name) VALUES ('p','legacy-unicode')")
            conn.execute(
                "INSERT INTO agent (id,name,profile_key) VALUES ('a','legacy','legacy')"
            )
            conn.execute(
                "INSERT INTO project_membership (project_id,agent_id,role) "
                "VALUES ('p','a','owner')"
            )
            content = unicodedata.normalize('NFD', 'café durable policy')
            old_fp = store._legacy_fingerprint(content)
            canonical_fp = core.fingerprint(content)
            self.assertNotEqual(old_fp, canonical_fp)
            memory_id, version_id = core.create_memory(
                conn, 'p', 'a', content, scope='project',
                idempotency_key=f'remember:p:a:{old_fp}'
            )
            conn.execute(
                'UPDATE memory SET claim_fingerprint=? WHERE id=?',
                (old_fp, memory_id)
            )
            core.reject(conn, memory_id, 'a', 'legacy unicode reject')
            conn.execute(
                "DELETE FROM schema_migrations WHERE version IN "
                "('0012_unicode_fingerprint_repair','0013_current_version_ownership',"
                "'0014_provenance_seal','0015_reinforcement_decay','0016_scope_detail','0017_bitemporal_valid_until','0018_memory_embedding')"
            )
        finally:
            conn.close()

        upgraded = store.open_store(legacy)
        try:
            canonical_fp = core.fingerprint('café durable policy')
            self.assertEqual(
                upgraded.execute(
                    'SELECT claim_fingerprint FROM memory WHERE id=?', (memory_id,)
                ).fetchone()[0],
                canonical_fp
            )
            self.assertIsNotNone(upgraded.execute(
                'SELECT 1 FROM tombstone WHERE claim_fingerprint=? AND overridden_by IS NULL',
                (canonical_fp,)
            ).fetchone())
            self.assertIsNotNone(upgraded.execute(
                'SELECT 1 FROM idempotency_key WHERE key=?',
                (f'remember:p:a:{canonical_fp}',)
            ).fetchone())
            with self.assertRaises(core.TombstoneBlocked):
                core.create_memory(
                    upgraded, 'p', 'a', 'café durable policy', scope='project'
                )
        finally:
            upgraded.close()

    def test_migration_history_gap_fails_closed(self):
        broken = os.path.join(self.tmp.name, 'migration-gap.db')
        conn = store.open_store(broken)
        try:
            conn.execute(
                "DELETE FROM schema_migrations WHERE version='0012_unicode_fingerprint_repair'"
            )
        finally:
            conn.close()

        with self.assertRaises(store.StoreError) as cm:
            store.open_store(broken)
        self.assertIn('invalid migration history', str(cm.exception))
        self.assertIn('0012_unicode_fingerprint_repair', str(cm.exception))

    def test_upgrade_to_0013_fails_closed_on_cross_memory_current_pointer(self):
        legacy = os.path.join(self.tmp.name, 'legacy-bad-pointer.db')
        conn = store.open_store(legacy)
        try:
            conn.execute("INSERT INTO project (id,name) VALUES ('p','bad-pointer')")
            conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('a','a','a')")
            conn.execute(
                "INSERT INTO project_membership (project_id,agent_id,role) "
                "VALUES ('p','a','owner')"
            )
            first_id, _ = core.create_memory(
                conn, 'p', 'a', 'first migration pointer', scope='project'
            )
            _second_id, second_ver = core.create_memory(
                conn, 'p', 'a', 'second migration pointer', scope='project'
            )
            for trigger in (
                'memory_current_version_owner_insert',
                'memory_current_version_owner_update',
                'memory_version_current_owner_insert',
            ):
                conn.execute(f'DROP TRIGGER {trigger}')
            # Remove both 0013 and 0014 so the runner replays them in order;
            # the ownership check inside 0013 must fire before 0014 runs.
            conn.execute(
                "DELETE FROM schema_migrations WHERE version IN "
                "('0013_current_version_ownership','0014_provenance_seal','0015_reinforcement_decay','0016_scope_detail','0017_bitemporal_valid_until','0018_memory_embedding')"
            )
            conn.execute(
                'UPDATE memory SET current_version_id=?, claim_fingerprint=? WHERE id=?',
                (second_ver, core.fingerprint('second migration pointer'), first_id)
            )
        finally:
            conn.close()

        with self.assertRaises(store.StoreError) as cm:
            store.open_store(legacy)
        self.assertIn('current-version ownership violations', str(cm.exception))

    def test_engine_writes_and_supersede_keep_fingerprint_index_current(self):
        memory_id, _ = core.create_memory(
            self.conn, 'proj', 'agent', 'Preferred editor is Helix', scope='private'
        )
        stored = self.conn.execute(
            'SELECT claim_fingerprint FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        self.assertEqual(stored, core.fingerprint('Preferred editor is Helix'))

        core.supersede(self.conn, memory_id, 'agent', 'Preferred editor is Zed')
        stored = self.conn.execute(
            'SELECT claim_fingerprint FROM memory WHERE id=?', (memory_id,)
        ).fetchone()[0]
        self.assertEqual(stored, core.fingerprint('Preferred editor is Zed'))

    def test_private_claim_lookup_uses_index_without_python_rescan(self):
        target_id = None
        for index in range(80):
            memory_id, _ = core.create_memory(
                self.conn, 'proj', 'agent', f'indexed private claim {index}', scope='private'
            )
            if index == 57:
                target_id = memory_id
        target_fp = core.fingerprint('indexed private claim 57')

        # Indexed rows should not need content fingerprinting at lookup time.
        with mock.patch.object(core, 'fingerprint', side_effect=AssertionError('slow scan')):
            found = ingest._find_private_claim(self.conn, 'proj', 'agent', target_fp)
        self.assertEqual(found, target_id)

        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT m.id FROM memory m "
            "WHERE m.project_id=? AND m.scope='private' AND m.owner_agent_id=? "
            "AND m.claim_fingerprint=? "
            "AND m.lifecycle NOT IN ('rejected','disabled','superseded')",
            ('proj', 'agent', target_fp)
        ).fetchall()
        self.assertIn('idx_memory_private_claim', ' '.join(str(row) for row in plan))

    def test_import_claim_lookup_uses_project_fingerprint_index_without_rescan(self):
        for index in range(80):
            core.create_memory(
                self.conn, 'proj', 'agent', f'project import claim {index}', scope='project'
            )
        target_fp = core.fingerprint('project import claim 57')
        with mock.patch.object(core, 'fingerprint', side_effect=AssertionError('slow scan')):
            self.assertTrue(
                core._claim_already_present(self.conn, 'proj', target_fp, scope='project')
            )
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM memory m "
            "WHERE m.project_id=? AND m.scope=? AND m.claim_fingerprint=? "
            "AND m.lifecycle != 'rejected' LIMIT 1",
            ('proj', 'project', target_fp)
        ).fetchall()
        self.assertIn('idx_memory_project_claim', ' '.join(str(row) for row in plan))

    def test_import_claim_lookup_scans_only_legacy_null_fingerprints(self):
        memory_id, _ = core.create_memory(
            self.conn, 'proj', 'agent', 'legacy import claim', scope='project'
        )
        self.conn.execute(
            'UPDATE memory SET claim_fingerprint=NULL WHERE id=?', (memory_id,)
        )
        self.assertTrue(core._claim_already_present(
            self.conn, 'proj', core.fingerprint('legacy import claim'), scope='project'
        ))

    def test_semantic_queue_query_uses_partial_index(self):
        event_id, _ = ingest.append_event(
            self.conn, 'proj', 'agent', 'turn', session_id='queue-index',
            user_content='This may become a durable project constraint.',
            assistant_content='Acknowledged.'
        )
        ingest.process_event(self.conn, event_id)
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT id,event_type,user_content,assistant_content,metadata,decision,created_at "
            "FROM ingest_event WHERE project_id=? AND agent_id=? AND status='pending' "
            "AND decision IN (?) ORDER BY created_at,id LIMIT ?",
            ('proj', 'agent', 'semantic_review_required', 5)
        ).fetchall()
        self.assertIn(
            'idx_ingest_event_pending_decision', ' '.join(str(row) for row in plan)
        )

    def test_runtime_openers_require_existing_store_and_skip_migrations(self):
        self.conn.close()
        with mock.patch.object(store, 'apply_migrations', side_effect=AssertionError('migration')):
            rw = store.open_runtime_store(self.db)
            try:
                self.assertEqual(rw.execute('SELECT COUNT(*) FROM project').fetchone()[0], 1)
            finally:
                rw.close()
            ro = store.open_runtime_store_readonly(self.db)
            try:
                self.assertEqual(ro.execute('SELECT COUNT(*) FROM project').fetchone()[0], 1)
                with self.assertRaises(Exception):
                    ro.execute("INSERT INTO project (id,name) VALUES ('x','x')")
            finally:
                ro.close()
        # Re-open for tearDown.
        self.conn = store.open_store(self.db)

        missing = os.path.join(self.tmp.name, 'missing.db')
        with self.assertRaises(store.StoreError):
            store.open_runtime_store(missing)
        with self.assertRaises(store.StoreError):
            store.open_runtime_store_readonly(missing)
        self.assertFalse(os.path.exists(missing))


if __name__ == '__main__':
    unittest.main()
