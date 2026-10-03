"""Tests for raw Hermes ingress journal and conservative analyzer."""
import os
import tempfile
import unittest

from memcore import core, ingest, store


class IngestTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_ingest_')
        self.db_path = os.path.join(self.tmpdir, 'ingest.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-demo'
        self.alice = 'agent-alice'
        self.bob = 'agent-bob'
        self.conn.execute("INSERT INTO project (id, name) VALUES (?, 'demo')", (self.project,))
        for aid, role in ((self.alice, 'owner'), (self.bob, 'member')):
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, aid.removeprefix('agent-'), aid.removeprefix('agent-'))
            )
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) VALUES (?, ?, ?)',
                (self.project, aid, role)
            )

    def tearDown(self):
        self.conn.close()
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass


class TestIngestJournal(IngestTestBase):
    def test_migration_creates_journal_tables(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        self.assertIn('ingest_event', names)
        self.assertIn('ingest_derivation', names)
        self.assertIn('ingest_analysis', names)
        self.assertEqual(store.MIGRATIONS[-1][0], '0016_scope_detail')

    def test_append_is_retry_safe(self):
        kwargs = dict(
            session_id='s1', user_content='remember that tea is preferred',
            assistant_content='Understood.', metadata={'message_count': 4}
        )
        first, created1 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', **kwargs
        )
        second, created2 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', **kwargs
        )
        self.assertEqual(first, second)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(self.conn.execute(
            'SELECT COUNT(*) FROM ingest_event').fetchone()[0], 1)

    def test_long_events_differing_after_storage_cap_do_not_deduplicate(self):
        prefix = 'x' * ingest.MAX_FIELD_CHARS
        first, created1 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id='long-payload', user_content=prefix + 'A'
        )
        second, created2 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id='long-payload', user_content=prefix + 'B'
        )
        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertNotEqual(first, second)
        rows = self.conn.execute(
            "SELECT user_content, content_hash FROM ingest_event "
            "WHERE session_id='long-payload' ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({len(row[0]) for row in rows}, {ingest.MAX_FIELD_CHARS})
        self.assertEqual(len({row[1] for row in rows}), 2)

    def test_long_session_ids_keep_distinct_suffixes_after_storage_cap(self):
        prefix = 's' * 512
        first, created1 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id=prefix + '-A', user_content='same payload'
        )
        second, created2 = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id=prefix + '-B', user_content='same payload'
        )
        self.assertTrue(created1)
        self.assertTrue(created2)
        self.assertNotEqual(first, second)
        session_ids = [row[0] for row in self.conn.execute(
            "SELECT session_id FROM ingest_event ORDER BY id"
        ).fetchall()]
        self.assertEqual(len(session_ids), 2)
        self.assertTrue(all(len(value) <= 512 for value in session_ids))
        self.assertEqual(len(set(session_ids)), 2)

    def test_nonmember_cannot_write_journal(self):
        self.conn.execute(
            "INSERT INTO agent (id, name, profile_key) VALUES ('agent-eve','eve','eve')"
        )
        with self.assertRaises(core.PermissionDenied):
            ingest.append_event(
                self.conn, self.project, 'agent-eve', 'turn', user_content='remember this'
            )

    def test_trivial_turn_is_ignored_without_memory(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id='s2', user_content='โอเค', assistant_content='ครับ'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_ambiguous_turn_stays_pending_and_is_not_recalled(self):
        text = 'project quasarphoenix may need a different deployment window tomorrow'
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn',
            session_id='s3', user_content=text, assistant_content='Noted.'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['decision'], 'semantic_review_required')
        self.assertEqual(core.search(
            self.conn, self.project, self.alice, 'quasarphoenix'
        ), [])

    def test_explicit_remember_becomes_private_accepted(self):
        # ADR-0019: explicit durable signals auto-accept (was candidate pre-0.7).
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s4',
            user_content='จำไว้ว่าฉันชอบชาอู่หลง', assistant_content='รับทราบ'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['decision'], 'private_accepted')
        row = self.conn.execute(
            'SELECT scope, lifecycle, owner_agent_id FROM memory WHERE id=?',
            (result['memory_id'],)
        ).fetchone()
        self.assertEqual(row, ('private', 'accepted', self.alice))
        content = self.conn.execute(
            'SELECT v.content FROM memory m JOIN memory_version v '
            'ON v.id=m.current_version_id WHERE m.id=?', (result['memory_id'],)
        ).fetchone()[0]
        self.assertEqual(content, 'ฉันชอบชาอู่หลง')

    def test_assistant_only_event_never_creates_memory(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s5',
            assistant_content='We decided to deploy every Friday.'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_same_claim_from_later_turn_links_as_duplicate(self):
        first, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s6',
            user_content='remember that I prefer dark mode',
            metadata={'message_count': 2}
        )
        r1 = ingest.process_event(self.conn, first)
        second, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s6',
            user_content='remember that I prefer dark mode',
            metadata={'message_count': 4}
        )
        r2 = ingest.process_event(self.conn, second)
        self.assertEqual(r1['memory_id'], r2['memory_id'])
        self.assertEqual(r2['decision'], 'duplicate')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 1)
        relation = self.conn.execute(
            'SELECT relation FROM ingest_derivation WHERE event_id=?', (second,)
        ).fetchone()[0]
        self.assertEqual(relation, 'duplicate')

    def test_analyzer_failure_preserves_raw_event(self):
        claim = 'I prefer forbidden zebra mode'
        fp = core.fingerprint(claim)
        self.conn.execute(
            'INSERT INTO tombstone (id, claim_fingerprint, scope, reason, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            ('ts-test', fp, f'private:{self.project}:{self.alice}', 'test', core._now())
        )
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='s7',
            user_content=claim
        )
        with self.assertRaises(core.TombstoneBlocked):
            ingest.process_event(self.conn, event_id)
        row = self.conn.execute(
            'SELECT status, user_content FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        self.assertEqual(row, ('failed', claim))

    def test_stats_exposes_journal_health(self):
        ingest.append_event(
            self.conn, self.project, self.alice, 'turn', session_id='stats',
            user_content='a normal ambiguous turn for later review'
        )
        stats = core.stats(self.conn)
        self.assertEqual(stats['journal'].get('pending'), 1)

    def _add_builtin_memory(self, content, *, target='memory', session_id='builtin-add'):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id=session_id,
            user_content=content, metadata={'action': 'add', 'target': target, 'success': True}
        )
        return ingest.process_event(self.conn, event_id)

    def test_builtin_remove_accepts_empty_content_and_rejects_exact_prior_hook_memory(self):
        added = self._add_builtin_memory('old built-in memory entry', session_id='rm-add')
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='rm1',
            metadata={
                'action': 'remove', 'target': 'memory',
                'old_text': 'old built-in memory entry', 'success': True,
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(result['decision'], 'builtin_memory_removed')
        self.assertEqual(result['memory_id'], added['memory_id'])
        lifecycle = self.conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (added['memory_id'],)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'rejected')
        self.assertEqual(core.search(
            self.conn, self.project, self.alice, 'old built-in memory entry'
        ), [])
        tombstone = self.conn.execute(
            'SELECT scope FROM tombstone WHERE claim_fingerprint=?',
            (core.fingerprint('old built-in memory entry'),)
        ).fetchone()
        self.assertEqual(
            tombstone[0], core._private_tombstone_scope(self.project, self.alice)
        )

    def test_builtin_replace_supersedes_exact_prior_hook_memory(self):
        added = self._add_builtin_memory('legacyzebra preference', target='user', session_id='rp-add')
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='rp1',
            user_content='freshquasar preference', metadata={
                'action': 'replace', 'target': 'user',
                'old_text': 'legacyzebra preference', 'success': True,
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(result['decision'], 'builtin_memory_replaced')
        self.assertEqual(result['memory_id'], added['memory_id'])
        audit_write_key = self.conn.execute(
            "SELECT write_key FROM audit_event WHERE action='supersede' AND memory_id=?",
            (added['memory_id'],)
        ).fetchone()[0]
        self.assertEqual(audit_write_key, f'ingest:{event_id}')
        row = self.conn.execute(
            'SELECT m.lifecycle, v.content FROM memory m JOIN memory_version v '
            'ON v.id=m.current_version_id WHERE m.id=?', (added['memory_id'],)
        ).fetchone()
        self.assertEqual(row, ('candidate', 'freshquasar preference'))
        self.assertEqual(core.search(
            self.conn, self.project, self.alice, 'legacyzebra'
        ), [])
        self.assertEqual(len(core.search(
            self.conn, self.project, self.alice, 'freshquasar'
        )), 1)

    def test_builtin_replace_accepts_nested_adapter_old_text(self):
        added = self._add_builtin_memory('nested legacy claim', session_id='nested-add')
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='nested-rp',
            user_content='nested current claim', metadata={
                'action': 'replace', 'target': 'memory',
                'builtin_metadata': {'old_text': 'nested legacy claim'},
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(result['decision'], 'builtin_memory_replaced')
        self.assertEqual(result['memory_id'], added['memory_id'])
        self.assertEqual(len(core.search(
            self.conn, self.project, self.alice, 'nested current claim'
        )), 1)

    def test_builtin_remove_journals_nested_old_text_without_content(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='nested-rm',
            metadata={
                'action': 'remove', 'target': 'memory',
                'builtin_metadata': {'old_text': 'nested remove reference'},
            }
        )
        row = self.conn.execute(
            'SELECT status FROM ingest_event WHERE id=?', (event_id,)
        ).fetchone()
        self.assertEqual(row[0], 'pending')

    def test_builtin_replace_without_exact_hook_origin_stays_pending(self):
        core.create_memory(
            self.conn, self.project, self.alice, 'conversation-derived claim',
            scope='private'
        )
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='rp-unresolved',
            user_content='replacement claim', metadata={
                'action': 'replace', 'target': 'memory',
                'old_text': 'conversation-derived claim', 'success': True,
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['decision'], 'builtin_memory_replace_unresolved_target')
        self.assertEqual(len(core.search(
            self.conn, self.project, self.alice, 'conversation-derived claim'
        )), 1)

    def test_builtin_remove_with_substring_reference_stays_pending(self):
        self._add_builtin_memory(
            'the complete durable memory claim', session_id='substring-add'
        )
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='substring-rm',
            metadata={
                'action': 'remove', 'target': 'memory',
                'old_text': 'durable memory', 'success': True,
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['decision'], 'builtin_memory_remove_unresolved_target')
        self.assertEqual(len(core.search(
            self.conn, self.project, self.alice, 'complete durable memory claim'
        )), 1)

    def test_failed_upstream_add_is_ignored_without_creating_memory(self):
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='failed-add-only',
            user_content='must not be persisted', metadata={
                'action': 'add', 'target': 'memory', 'success': False,
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(result['decision'], 'builtin_memory_write_failed_upstream')
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)

    def test_mutation_replay_recovers_from_audit_marker(self):
        added = self._add_builtin_memory('recoverable old claim', session_id='recover-add')
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='recover-rp',
            user_content='recoverable new claim', metadata={
                'action': 'replace', 'target': 'memory',
                'old_text': 'recoverable old claim', 'success': True,
            }
        )
        core.supersede(
            self.conn, added['memory_id'], self.alice, 'recoverable new claim',
            reason=f'ingest:{event_id}'
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(result['decision'], 'builtin_memory_replaced')
        self.assertEqual(result['memory_id'], added['memory_id'])
        relation = self.conn.execute(
            'SELECT relation FROM ingest_derivation WHERE event_id=?', (event_id,)
        ).fetchone()[0]
        self.assertEqual(relation, 'corrected')

    def test_failed_upstream_memory_write_is_ignored(self):
        self._add_builtin_memory('keep this built-in entry', session_id='failed-add')
        event_id, _ = ingest.append_event(
            self.conn, self.project, self.alice, 'memory_write', session_id='failed-rm',
            metadata={
                'action': 'remove', 'target': 'memory',
                'old_text': 'keep this built-in entry', 'success': False,
            }
        )
        result = ingest.process_event(self.conn, event_id)
        self.assertEqual(result['status'], 'ignored')
        self.assertEqual(result['decision'], 'builtin_memory_write_failed_upstream')
        self.assertEqual(len(core.search(
            self.conn, self.project, self.alice, 'keep this built-in entry'
        )), 1)
