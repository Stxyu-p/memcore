"""Regression tests for MemCore quality hardening:

- Unicode NFC normalization in fingerprinting
- Journal pruning and cascade cleanup
- Unresolved event dismissal and journal health recovery
- Plugin connection pool dead-thread eviction
- Private scope security isolation
"""
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unicodedata
import unittest

from memcore import core, ingest, store
from integrations.hermes.memcore import plugin


class QualityHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_quality_')
        self.db_path = str(pathlib.Path(self.tmp.name) / 'memory.db')
        self.conn = store.open_store(self.db_path)
        self.conn.execute("INSERT INTO project (id, name) VALUES ('proj-main', 'main')")
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-alice', 'alice', 'alice')")
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-bob', 'bob', 'bob')")
        self.conn.execute("INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-main', 'agent-alice', 'owner')")
        self.conn.execute("INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-main', 'agent-bob', 'member')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        plugin.reset_conn()
        self.tmp.cleanup()

    # ── 1. Unicode NFC Normalization ───────────────────────────────────────

    def test_fingerprint_nfc_consistency_for_thai_and_combining_chars(self):
        composed_thai = 'จำไว้ว่าระบบทำงานได้ดี'
        decomposed_thai = unicodedata.normalize('NFD', composed_thai)
        self.assertEqual(core.fingerprint(composed_thai), core.fingerprint(decomposed_thai))

        composed_accent = 'café résumé'
        decomposed_accent = unicodedata.normalize('NFD', composed_accent)
        self.assertEqual(core.fingerprint(composed_accent), core.fingerprint(decomposed_accent))

    def test_tombstone_blocks_decomposed_unicode_variant(self):
        composed = 'ข้อตกลงเรื่องรูปแบบโค้ด'
        decomposed = unicodedata.normalize('NFD', composed)

        mem_id, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', composed, scope='project'
        )
        core.reject(self.conn, mem_id, 'agent-alice', 'outdated standard')

        # Trying to admit the decomposed variant must be blocked by the active tombstone
        self.assertFalse(core.admission_allowed(self.conn, decomposed, 'proj-main', scope='project'))
        with self.assertRaises(core.TombstoneBlocked):
            core.create_memory(self.conn, 'proj-main', 'agent-alice', decomposed, scope='project')

    def test_tombstone_hides_existing_accepted_duplicate_from_recall(self):
        content = 'duplicate claim must disappear after rejection'
        first, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', content, scope='project'
        )
        second, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', content, scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET lifecycle='accepted' WHERE id IN (?, ?)",
            (first, second)
        )
        self.conn.commit()
        core.reject(self.conn, second, 'agent-alice', 'claim disproven')

        self.assertEqual(
            core.search(self.conn, 'proj-main', 'agent-alice', 'duplicate claim'), []
        )
        self.assertEqual(
            core.visible_memories(self.conn, 'proj-main', 'agent-alice'), []
        )
        # The reject sweeps the same-claim duplicate into rejected too, so
        # both rows are in the rejected history — recall stays clean.
        historical = core.visible_memories(
            self.conn, 'proj-main', 'agent-alice', include_rejected=True
        )
        self.assertEqual(
            sorted(row[0] for row in historical), sorted([first, second]))

    def test_null_fingerprint_memory_fails_closed_from_recall(self):
        memory_id, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice',
            'legacy row missing indexed claim identity', scope='project'
        )
        self.conn.execute(
            "UPDATE memory SET lifecycle='accepted', claim_fingerprint=NULL WHERE id=?",
            (memory_id,)
        )
        self.conn.commit()
        self.assertEqual(
            core.search(self.conn, 'proj-main', 'agent-alice', 'legacy row'), []
        )
        self.assertFalse(any(
            row[0] == memory_id
            for row in core.visible_memories(self.conn, 'proj-main', 'agent-alice')
        ))

    # ── 2. Ingest Journal Pruning & GC ─────────────────────────────────────

    def test_prune_journal_removes_old_processed_and_ignored_only(self):
        old_time = '2020-01-01T00:00:00Z'
        new_time = core._now()

        # Insert events with old and new timestamps across statuses
        events = [
            ('evt-old-processed', old_time, 'processed', 'candidate'),
            ('evt-old-ignored', old_time, 'ignored', 'ignore'),
            ('evt-old-pending', old_time, 'pending', 'builtin_memory_remove_unresolved_target'),
            ('evt-new-processed', new_time, 'processed', 'candidate'),
        ]
        dummy_mem, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', 'dummy memory for derivation', scope='project'
        )
        for eid, ts, st, dec in events:
            self.conn.execute(
                "INSERT INTO ingest_event (id, project_id, agent_id, event_type, user_content, content_hash, status, decision, created_at) "
                "VALUES (?, 'proj-main', 'agent-alice', 'turn', 'content', ?, ?, ?, ?)",
                (eid, f'hash-{eid}', st, dec, ts)
            )
            if 'processed' in st:
                self.conn.execute(
                    "INSERT INTO ingest_derivation (event_id, memory_id, relation, created_at) "
                    "VALUES (?, ?, 'created', ?)", (eid, dummy_mem, ts)
                )

        self.conn.commit()

        # Prune events older than 30 days
        pruned = ingest.prune_journal(self.conn, days=30, statuses=('ignored', 'processed'))
        self.assertEqual(pruned, 2)

        remaining_ids = {row[0] for row in self.conn.execute('SELECT id FROM ingest_event').fetchall()}
        self.assertIn('evt-old-pending', remaining_ids, 'Pending events must NEVER be pruned')
        self.assertIn('evt-new-processed', remaining_ids, 'Recent events must not be pruned')
        self.assertNotIn('evt-old-processed', remaining_ids)
        self.assertNotIn('evt-old-ignored', remaining_ids)

        # Confirm derivations for pruned events were cleanly deleted
        derivations = self.conn.execute('SELECT event_id FROM ingest_derivation').fetchall()
        self.assertEqual(derivations, [('evt-new-processed',)])

    def test_prune_journal_refuses_pending_status(self):
        with self.assertRaises(core.MemCoreError) as cm:
            ingest.prune_journal(self.conn, days=10, statuses=('pending', 'processed'))
        self.assertIn('cannot prune journal events with status: pending', str(cm.exception))

    # ── 3. Unresolved Builtin Event Dismissal ───────────────────────────────

    def test_dismiss_unresolved_event_recovers_health(self):
        eid, _ = ingest.append_event(
            self.conn, 'proj-main', 'agent-alice', 'memory_write',
            user_content='delete old fact', metadata={'action': 'remove', 'old_text': 'non-existent'}
        )
        ingest.process_event(self.conn, eid)

        stats_before = ingest.journal_stats(self.conn)
        self.assertEqual(stats_before['health'], 'operator_attention')
        self.assertEqual(stats_before['unresolved_builtin_mutations'], 1)

        # Alice dismisses the event
        res = ingest.dismiss_unresolved_event(self.conn, eid, 'agent-alice', rationale='verified obsolete')
        self.assertEqual(res['status'], 'ignored')

        stats_after = ingest.journal_stats(self.conn)
        self.assertEqual(stats_after['health'], 'ok')
        self.assertEqual(stats_after['unresolved_builtin_mutations'], 0)

        # Audit event was recorded
        audit = self.conn.execute(
            "SELECT action, actor_agent_id, detail FROM audit_event WHERE action='journal_dismiss'"
        ).fetchone()
        self.assertIsNotNone(audit)
        self.assertEqual(audit[0], 'journal_dismiss')
        self.assertEqual(audit[1], 'agent-alice')
        self.assertIn('verified obsolete', audit[2])

    def test_dismiss_unresolved_event_rejects_non_member(self):
        eid, _ = ingest.append_event(
            self.conn, 'proj-main', 'agent-alice', 'turn', user_content='test turn'
        )
        # Random non-member agent cannot dismiss
        self.conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-eve', 'eve', 'eve')")
        with self.assertRaises(core.PermissionDenied):
            ingest.dismiss_unresolved_event(self.conn, eid, 'agent-eve')

    def test_member_cannot_dismiss_another_agents_review_event(self):
        eid, _ = ingest.append_event(
            self.conn, 'proj-main', 'agent-alice', 'turn',
            user_content='This may become a durable preference later.'
        )
        ingest.process_event(self.conn, eid)
        with self.assertRaises(core.PermissionDenied):
            ingest.dismiss_unresolved_event(self.conn, eid, 'agent-bob')
        self.assertEqual(
            self.conn.execute(
                'SELECT status, decision FROM ingest_event WHERE id=?', (eid,)
            ).fetchone(),
            ('pending', 'semantic_review_required')
        )

    def test_unclassified_pending_event_cannot_be_dismissed(self):
        eid, _ = ingest.append_event(
            self.conn, 'proj-main', 'agent-alice', 'turn',
            user_content='raw event not processed yet'
        )
        with self.assertRaises(core.MemCoreError):
            ingest.dismiss_unresolved_event(self.conn, eid, 'agent-alice')
        self.assertEqual(
            self.conn.execute(
                'SELECT status, decision FROM ingest_event WHERE id=?', (eid,)
            ).fetchone(),
            ('pending', None)
        )

    # ── 4. Connection Pool Dead Thread Eviction ────────────────────────────

    def test_plugin_connection_pool_evicts_dead_threads(self):
        created_connections = []
        barrier = threading.Barrier(6)

        def worker():
            c = plugin._get_conn(self.db_path)
            created_connections.append(c)
            barrier.wait()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Calling _get_conn in main thread triggers dead thread eviction
        main_conn = plugin._get_conn(self.db_path)
        with plugin._connections_lock:
            # Dead threads should have been evicted; only active threads remain
            active_tids = {t.ident for t in threading.enumerate()}
            for tid in plugin._connections:
                self.assertIn(tid, active_tids)
            self.assertIn(threading.get_ident(), plugin._connections)

    def test_dead_worker_transaction_does_not_keep_database_locked(self):
        plugin.reset_conn()
        state = {}

        def worker():
            conn = plugin._get_conn(self.db_path)
            conn.execute('BEGIN IMMEDIATE')
            state['conn'] = conn
            state['tid'] = threading.get_ident()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertTrue(state['conn'].in_transaction)
        self.assertNotEqual(state['tid'], threading.get_ident())

        # A subsequent plugin access must evict/close the dead worker handle
        # even when the pool contains fewer than five entries.
        main_conn = plugin._get_conn(self.db_path)
        main_conn.execute('PRAGMA busy_timeout=50')
        main_conn.execute('BEGIN IMMEDIATE')
        main_conn.execute('ROLLBACK')
        with self.assertRaises(Exception):
            state['conn'].execute('SELECT 1')

    # ── 5. Private Scope Security Isolation ────────────────────────────────

    def test_current_version_pointer_cannot_cross_memory_ownership(self):
        self.conn.execute("INSERT INTO project (id,name) VALUES ('proj-secret','secret')")
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-secret','agent-alice','owner')"
        )
        public_id, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', 'public pointer target',
            scope='project', lifecycle='accepted'
        )
        _secret_id, secret_ver = core.create_memory(
            self.conn, 'proj-secret', 'agent-alice', 'private cross-project secret',
            scope='private', lifecycle='accepted'
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                'UPDATE memory SET current_version_id=? WHERE id=?',
                (secret_ver, public_id)
            )

    def test_reads_fail_closed_on_legacy_cross_memory_pointer(self):
        self.conn.execute("INSERT INTO project (id,name) VALUES ('proj-secret','secret')")
        self.conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-secret','agent-alice','owner')"
        )
        public_id, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', 'public harmless content',
            scope='project', lifecycle='accepted'
        )
        _secret_id, secret_ver = core.create_memory(
            self.conn, 'proj-secret', 'agent-alice', 'private cross-project secret',
            scope='private', lifecycle='accepted'
        )
        self.conn.execute('DROP TRIGGER memory_current_version_owner_update')
        self.conn.execute(
            'UPDATE memory SET current_version_id=?, claim_fingerprint=? WHERE id=?',
            (secret_ver, core.fingerprint('private cross-project secret'), public_id)
        )
        visible_ids = {
            row[0] for row in core.visible_memories(
                self.conn, 'proj-main', 'agent-alice'
            )
        }
        self.assertNotIn(public_id, visible_ids)

    def test_private_memory_cannot_be_read_or_modified_by_other_agent(self):
        alice_mem, _ = core.create_memory(
            self.conn, 'proj-main', 'agent-alice', 'alice private notes', scope='private'
        )

        # Bob cannot see Alice's private memory in visible_memories
        bob_visible = core.visible_memories(self.conn, 'proj-main', 'agent-bob')
        self.assertEqual(bob_visible, [])

        # Bob cannot deactivate Alice's private memory
        with self.assertRaises(core.PermissionDenied):
            core.deactivate(self.conn, alice_mem, 'agent-bob')

        # Bob cannot supersede Alice's private memory
        with self.assertRaises(core.PermissionDenied):
            core.supersede_memory(self.conn, alice_mem, 'agent-bob', 'tampered content')

        # Bob cannot promote Alice's private memory
        with self.assertRaises(core.PermissionDenied):
            core.promote(self.conn, alice_mem, 'agent-bob')

    # ── 6. CLI journal-dismiss and gc --journal-days ───────────────────────

    def test_cli_journal_dismiss_and_gc_journal_days(self):
        from memcore import __main__ as cli
        import contextlib
        import io

        eid, _ = ingest.append_event(
            self.conn, 'proj-main', 'agent-alice', 'turn',
            user_content='cli test turn'
        )
        ingest.process_event(self.conn, eid)
        self.conn.commit()

        # Run journal-dismiss via CLI
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(['--db', self.db_path, 'journal-dismiss', eid, '--agent', 'alice', '--rationale', 'cli dismissed'])
        self.assertIn('"status": "ignored"', out.getvalue())

        # Verify event was ignored in db
        status = self.conn.execute('SELECT status FROM ingest_event WHERE id=?', (eid,)).fetchone()[0]
        self.assertEqual(status, 'ignored')

        # Test gc --journal-days 0 --apply (prunes the ignored event)
        out_gc = io.StringIO()
        with contextlib.redirect_stdout(out_gc):
            cli.main(['--db', self.db_path, 'gc', '--journal-days', '0', '--apply'])
        self.assertIn('journal: 1 historical events pruned', out_gc.getvalue())
        remaining = self.conn.execute('SELECT COUNT(*) FROM ingest_event WHERE id=?', (eid,)).fetchone()[0]
        self.assertEqual(remaining, 0)


if __name__ == '__main__':
    unittest.main()
