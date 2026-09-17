"""Native MemoryProvider integration tests against a real temp MemCore store."""
import json
import os
import pathlib
import tempfile
import unittest
import sys

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
for path in (REPO_ROOT, PLUGIN_ROOT):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)

from memcore import core, ingest, store
from native_provider import MemCoreMemoryProvider, agent_plugin


def config_for(path, agent='alice'):
    return {
        'plugins': {'entries': {'memcore': {'settings': {
            'store_path': path,
            'default_project': 'demo',
            'agent_name': agent,
            'inject': {'budget_chars': 1200, 'max_items': 8},
        }}}}
    }


class FakePluginLlm:
    def __init__(self, parsed=None, error=None):
        self.parsed = parsed or {
            'verdict': 'ignore',
            'candidate_content': '',
            'confidence': 0.95,
            'rationale': 'Transient detail.',
        }
        self.error = error
        self.calls = []

    def complete_structured(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        usage = type('Usage', (), {
            'input_tokens': 120,
            'output_tokens': 24,
            'total_tokens': 144,
        })()
        return type('Result', (), {
            'parsed': dict(self.parsed),
            'text': json.dumps(self.parsed),
            'provider': 'fake-host',
            'model': 'fake-model-v1',
            'usage': usage,
        })()


class NativeProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_native_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo','demo')")
        conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-alice','alice','alice')")
        conn.execute("INSERT INTO project_membership (project_id, agent_id, role) VALUES ('proj-demo','agent-alice','owner')")
        conn.close()

    def provider(self):
        p = MemCoreMemoryProvider()
        p._load_config = lambda: config_for(self.db)
        p.initialize('session-1', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        return p

    def auto_provider(self, llm, **overrides):
        config = config_for(self.db)
        auto = {
            'enabled': True,
            'max_events_per_turn': 1,
            'max_tokens': 256,
            'timeout_seconds': 10.0,
            'max_input_chars': 4000,
            'min_remember_confidence': 0.85,
            'failure_threshold': 2,
            'cooldown_seconds': 60.0,
        }
        auto.update(overrides)
        config['plugins']['entries']['memcore']['settings']['semantic'] = {
            'auto_review': auto
        }
        p = MemCoreMemoryProvider(plugin_llm=llm)
        p._load_config = lambda: config
        p.initialize('session-auto', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        return p

    def tearDown(self):
        agent_plugin.reset_conn()
        self.tmp.cleanup()

    def test_provider_contract_and_tools(self):
        p = self.provider()
        self.assertEqual(p.name, 'memcore')
        self.assertTrue(p.is_available())
        names = {s['name'] for s in p.get_tool_schemas()}
        self.assertEqual(names, {
            'memory_remember', 'memory_search', 'memory_promote',
            'memory_supersede', 'memory_reject', 'memory_feedback',
            'memory_review_queue', 'memory_review_decide'
        })
        self.assertIn('journal', p.system_prompt_block().lower())
        self.assertIn('untrusted historical data', p.system_prompt_block().lower())

    def test_unrelated_pins_do_not_hide_query_hits(self):
        conn = store.open_store(self.db)
        for i in range(8):
            mid, _ = core.create_memory(conn, 'proj-demo', 'agent-alice',
                                       'unrelated ' + str(i) + 'x' * 240, scope='project')
            conn.execute('UPDATE memory SET pinned=1 WHERE id=?', (mid,))
        core.create_memory(conn, 'proj-demo', 'agent-alice',
                           'nebula endpoint uses port 4890', scope='project')
        conn.close()
        p = self.provider()
        self.assertIn('nebula endpoint uses port 4890', p.prefetch('nebula'))
        self.assertNotIn('unrelated', p.prefetch('nebula'))
        self.assertEqual(p.prefetch('unknownquery'), '')
        conn = store.open_store(self.db)
        mid, _ = core.create_memory(conn, 'proj-demo', 'agent-alice',
                                   'important global constraint', scope='project')
        conn.execute('UPDATE memory SET pinned=1, critical=1 WHERE id=?', (mid,))
        conn.close()
        self.assertIn('important global constraint', p.prefetch('unknownquery'))

    def test_prefetch_preserves_governed_trust_labels(self):
        conn = store.open_store(self.db)
        candidate, _ = core.create_memory(
            conn, 'proj-demo', 'agent-alice', 'nebula candidate marker', scope='project'
        )
        accepted, _ = core.create_memory(
            conn, 'proj-demo', 'agent-alice', 'nebula accepted marker', scope='project'
        )
        conn.execute("UPDATE memory SET lifecycle='accepted' WHERE id=?", (accepted,))
        conn.close()
        p = self.provider()
        out = p.prefetch('nebula marker')
        self.assertIn('nebula accepted marker', out)
        self.assertIn('nebula candidate marker', out)
        self.assertIn('candidate | unverified | current', out)
        self.assertIn('accepted | unverified | current', out)
        self.assertIn('per-item MemCore labels are the governing trust semantics',
                      p.system_prompt_block())
        self.assertEqual(p.recall_status().count, 2)

    def test_recall_status_counts_only_complete_rendered_facts(self):
        conn = store.open_store(self.db)
        for content in ('nebula ' + 'x' * 400, 'nebula short fact'):
            core.create_memory(conn, 'proj-demo', 'agent-alice', content, scope='project')
        conn.close()
        p = self.provider()
        p._budget = 180
        out = p.prefetch('nebula')
        self.assertIn('nebula short fact', out)
        self.assertNotIn('xxx', out)
        self.assertEqual(p.recall_status().count, 1)

    def test_prefetch_excludes_pinned_duplicate_blocked_by_tombstone(self):
        conn = store.open_store(self.db)
        try:
            content = 'pinned duplicate claim is no longer trusted'
            pinned, _ = core.create_memory(
                conn, 'proj-demo', 'agent-alice', content, scope='project'
            )
            rejected, _ = core.create_memory(
                conn, 'proj-demo', 'agent-alice', content, scope='project'
            )
            conn.execute(
                "UPDATE memory SET lifecycle='accepted', pinned=1 WHERE id=?",
                (pinned,)
            )
            conn.execute(
                "UPDATE memory SET lifecycle='accepted' WHERE id=?",
                (rejected,)
            )
            core.reject(conn, rejected, 'agent-alice', 'claim disproven')
        finally:
            conn.close()

        p = self.provider()
        self.assertEqual(p.prefetch('pinned duplicate claim'), '')
        self.assertIsNone(p.recall_status())

    def test_prefetch_skips_trivial_greeting(self):
        conn = store.open_store(self.db)
        core.create_memory(conn, 'proj-demo', 'agent-alice',
                           'nebula trivial skip marker', scope='project')
        conn.close()
        p = self.provider()
        self.assertEqual(p.prefetch('สวัสดีค่ะ'), '')
        self.assertIsNone(p.recall_status())
        self.assertIn('nebula trivial skip marker',
                      p.prefetch('nebula trivial skip marker'))

    def test_sync_turn_journals_before_admission(self):
        p = self.provider()
        p.sync_turn(
            'project auroracat may change next week', 'I can review that.',
            session_id='session-1', messages=[{'role': 'user'}, {'role': 'assistant'}]
        )
        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                "SELECT status, decision FROM ingest_event WHERE event_type='turn'"
            ).fetchone()
            self.assertEqual(row, ('pending', 'semantic_review_required'))
            self.assertEqual(core.search(
                conn, 'proj-demo', 'agent-alice', 'auroracat'
            ), [])
        finally:
            conn.close()

    def test_auto_semantic_review_remembers_only_private_candidate(self):
        llm = FakePluginLlm({
            'verdict': 'remember',
            'candidate_content': 'deployment target uses the migration-safe path',
            'confidence': 0.94,
            'rationale': 'Durable project operating constraint.',
        })
        p = self.auto_provider(llm)
        p.sync_turn(
            'The deployment target uses the migration-safe path now.',
            'Acknowledged.', session_id='auto-remember'
        )
        self.assertEqual(len(llm.calls), 1)
        call = llm.calls[0]
        self.assertEqual(call['purpose'], 'memory.semantic-review')
        self.assertIn('UNTRUSTED HISTORICAL DATA', call['instructions'])
        self.assertEqual(call['max_tokens'], 256)
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT status,decision FROM ingest_event WHERE session_id='auto-remember'"
            ).fetchone()
            self.assertEqual(event, ('processed', 'semantic_private_candidate'))
            memory = conn.execute(
                'SELECT m.scope,m.lifecycle,m.owner_agent_id,v.content FROM memory m '
                'JOIN memory_version v ON v.id=m.current_version_id'
            ).fetchone()
            self.assertEqual(memory, (
                'private', 'candidate', 'agent-alice',
                'deployment target uses the migration-safe path'
            ))
            analyzer, verdict, confidence, raw_metadata = conn.execute(
                'SELECT analyzer,verdict,confidence,metadata FROM ingest_analysis'
            ).fetchone()
            self.assertEqual(analyzer, 'hermes-plugin-llm:alice')
            self.assertEqual((verdict, confidence), ('remember', 0.94))
            metadata = json.loads(raw_metadata)
            self.assertTrue(metadata['automatic'])
            self.assertEqual(metadata['analyzer']['provider'], 'fake-host')
            self.assertEqual(metadata['analyzer']['model'], 'fake-model-v1')
        finally:
            conn.close()

    def test_auto_semantic_review_low_confidence_remember_defers_once(self):
        llm = FakePluginLlm({
            'verdict': 'remember',
            'candidate_content': 'possibly durable but uncertain claim',
            'confidence': 0.62,
            'rationale': 'The wording may be temporary.',
        })
        p = self.auto_provider(llm, min_remember_confidence=0.85)
        p.sync_turn(
            'This setting may become our standard after more testing.',
            'We can revisit it.', session_id='auto-defer'
        )
        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                "SELECT status,decision FROM ingest_event WHERE session_id='auto-defer'"
            ).fetchone()
            self.assertEqual(row, ('pending', 'semantic_deferred'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)
        finally:
            conn.close()
        self.assertEqual(len(llm.calls), 1)

        # A later turn must not spend another model call on an automatically
        # deferred event. Deferred items are left for explicit/manual review.
        p.sync_turn(
            'remember that compact mode is preferred', 'Noted.',
            session_id='auto-defer-trigger'
        )
        self.assertEqual(len(llm.calls), 1)

    def test_auto_semantic_review_provider_failure_leaves_event_pending(self):
        llm = FakePluginLlm(error=RuntimeError('host model unavailable'))
        p = self.auto_provider(llm)
        p.sync_turn(
            'The release process now has a potentially durable exception.',
            'I will note that.', session_id='auto-failure'
        )
        self.assertEqual(len(llm.calls), 1)
        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                "SELECT status,decision FROM ingest_event WHERE session_id='auto-failure'"
            ).fetchone()
            self.assertEqual(row, ('pending', 'semantic_review_required'))
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ingest_analysis').fetchone()[0], 0)
        finally:
            conn.close()

    def test_auto_semantic_review_circuit_breaker_suppresses_repeated_failures(self):
        llm = FakePluginLlm(error=RuntimeError('host model unavailable'))
        p = self.auto_provider(
            llm, failure_threshold=2, cooldown_seconds=60.0,
            max_events_per_turn=1
        )
        for index in range(3):
            p.sync_turn(
                f'Ambiguous durable detail during outage {index}.',
                'Acknowledged.', session_id=f'circuit-{index}'
            )
        self.assertEqual(len(llm.calls), 2)
        self.assertGreater(p._semantic_circuit_open_until, 0.0)
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ingest_event "
                "WHERE decision='semantic_review_required' AND status='pending'"
            ).fetchone()[0], 3)
        finally:
            conn.close()

    def test_auto_semantic_review_circuit_half_open_probe_recovers(self):
        llm = FakePluginLlm(error=RuntimeError('host model unavailable'))
        p = self.auto_provider(llm, failure_threshold=1, cooldown_seconds=60.0)
        p.sync_turn(
            'Ambiguous durable detail while backend is offline.',
            'Acknowledged.', session_id='circuit-open'
        )
        self.assertEqual(len(llm.calls), 1)
        self.assertGreater(p._semantic_circuit_open_until, 0.0)

        # Simulate cooldown expiry and backend recovery. One probe is allowed;
        # a successful result closes the circuit and clears failure history.
        p._semantic_circuit_open_until = 0.0
        llm.error = None
        p.sync_turn(
            'Another ambiguous durable detail after backend recovery.',
            'Acknowledged.', session_id='circuit-recover'
        )
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(p._semantic_consecutive_failures, 0)
        self.assertEqual(p._semantic_circuit_open_until, 0.0)

    def test_auto_semantic_review_is_opt_in(self):
        llm = FakePluginLlm()
        p = MemCoreMemoryProvider(plugin_llm=llm)
        p._load_config = lambda: config_for(self.db)
        p.initialize('opt-in', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        p.sync_turn(
            'This ambiguous detail should stay queued without opt in.',
            'Okay.', session_id='auto-disabled'
        )
        self.assertEqual(llm.calls, [])
        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                "SELECT status,decision FROM ingest_event WHERE session_id='auto-disabled'"
            ).fetchone()
            self.assertEqual(row, ('pending', 'semantic_review_required'))
        finally:
            conn.close()

    def test_builtin_memory_write_never_triggers_auto_semantic_llm(self):
        llm = FakePluginLlm()
        p = self.auto_provider(llm)
        p.on_memory_write('add', 'user', 'Preferred editor is Helix', {'tool_name': 'memory'})
        self.assertEqual(llm.calls, [])

    def test_auto_semantic_review_drains_only_bounded_backlog_slice(self):
        conn = store.open_store(self.db)
        try:
            for index in range(2):
                event_id, _ = ingest.append_event(
                    conn, 'proj-demo', 'agent-alice', 'turn',
                    session_id=f'backlog-{index}',
                    user_content=f'Ambiguous durable backlog observation {index}.',
                    assistant_content='Acknowledged.'
                )
                ingest.process_event(conn, event_id)
        finally:
            conn.close()

        llm = FakePluginLlm({
            'verdict': 'ignore',
            'candidate_content': '',
            'confidence': 0.91,
            'rationale': 'Not durable enough.',
        })
        p = self.auto_provider(llm, max_events_per_turn=1)
        p.sync_turn(
            'remember that bounded review is enabled', 'Noted.',
            session_id='backlog-trigger'
        )
        self.assertEqual(len(llm.calls), 1)
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ingest_event WHERE decision='semantic_ignored'"
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ingest_event WHERE decision='semantic_review_required'"
            ).fetchone()[0], 1)
        finally:
            conn.close()

    def test_invalid_auto_semantic_config_disables_only_auto_review(self):
        llm = FakePluginLlm()
        p = self.auto_provider(llm, max_events_per_turn=0)
        self.assertFalse(p._semantic_auto_enabled)
        p.sync_turn(
            'This event remains reviewable even with invalid auto config.',
            'Okay.', session_id='invalid-auto-config'
        )
        self.assertEqual(llm.calls, [])
        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                "SELECT status,decision FROM ingest_event WHERE session_id='invalid-auto-config'"
            ).fetchone()
            self.assertEqual(row, ('pending', 'semantic_review_required'))
        finally:
            conn.close()

    def test_semantic_review_tools_curate_pending_journal(self):
        p = self.provider()
        p.sync_turn(
            'project auroracat may change after the migration window',
            'I can review that later.', session_id='semantic-tool-session'
        )
        queued = json.loads(p.handle_tool_call('memory_review_queue', {'limit': 5}))
        self.assertTrue(queued['success'], queued)
        self.assertEqual(len(queued['events']), 1)
        event_id = queued['events'][0]['event_id']
        self.assertIn('auroracat', queued['events'][0]['user_content'])
        decided = json.loads(p.handle_tool_call(
            'memory_review_decide', {
                'event_id': event_id,
                'verdict': 'remember',
                'content': 'auroracat migration window is operationally significant',
                'confidence': 0.9,
                'rationale': 'Stable project constraint worth retaining as a candidate.',
            }
        ))
        self.assertTrue(decided['success'], decided)
        self.assertEqual(decided['decision'], 'semantic_private_candidate')
        empty = json.loads(p.handle_tool_call('memory_review_queue', {}))
        self.assertEqual(empty['events'], [])
        conn = store.open_store(self.db)
        try:
            results = core.search(
                conn, 'proj-demo', 'agent-alice', 'auroracat migration'
            )
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1:3], ('private', 'candidate'))
        finally:
            conn.close()

    def test_explicit_sync_turn_derives_private_candidate(self):
        p = self.provider()
        p.sync_turn('remember that I prefer oolong tea', 'Understood.',
                    session_id='session-2', messages=[{'role': 'user'}])
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT status, decision FROM ingest_event WHERE session_id='session-2'"
            ).fetchone()
            self.assertEqual(event, ('processed', 'private_candidate'))
            memory = conn.execute(
                "SELECT scope, lifecycle, owner_agent_id FROM memory"
            ).fetchone()
            self.assertEqual(memory, ('private', 'candidate', 'agent-alice'))
        finally:
            conn.close()

    def test_sync_retry_does_not_duplicate_event_or_memory(self):
        p = self.provider()
        messages = [{'role': 'user'}, {'role': 'assistant'}]
        for _ in range(2):
            p.sync_turn('remember that I use compact mode', 'Noted.',
                        session_id='session-3', messages=messages)
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM ingest_event').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 1)
        finally:
            conn.close()

    def test_builtin_memory_write_is_explicit_private_candidate(self):
        p = self.provider()
        p.on_memory_write('add', 'user', 'Preferred editor is Helix', {'tool_name': 'memory'})
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT event_type, status, decision FROM ingest_event"
            ).fetchone()
            self.assertEqual(event, ('memory_write', 'processed', 'private_candidate'))
            content = conn.execute(
                'SELECT v.content FROM memory m JOIN memory_version v '
                'ON v.id=m.current_version_id'
            ).fetchone()[0]
            self.assertEqual(content, 'Preferred editor is Helix')
        finally:
            conn.close()

    def test_native_tool_routes_to_existing_governed_handler(self):
        p = self.provider()
        out = json.loads(p.handle_tool_call(
            'memory_remember', {'content': 'native tool marker'}
        ))
        self.assertTrue(out['success'])
        result = json.loads(p.handle_tool_call(
            'memory_search', {'query': 'native tool marker'}
        ))
        self.assertTrue(result['success'])
        self.assertTrue(any('native tool marker' in r['content'] for r in result['results']))

    def test_builtin_memory_remove_rejects_exact_mirrored_claim(self):
        p = self.provider()
        p.on_memory_write(
            'add', 'user', 'Preferred editor is Helix', {'tool_name': 'memory'}
        )
        p.on_memory_write(
            'remove', 'user', '',
            {'tool_name': 'memory', 'old_text': 'Preferred editor is Helix'}
        )
        conn = store.open_store(self.db)
        try:
            events = conn.execute(
                "SELECT status, decision FROM ingest_event ORDER BY created_at, rowid"
            ).fetchall()
            self.assertEqual(events, [
                ('processed', 'private_candidate'),
                ('processed', 'builtin_memory_removed'),
            ])
            lifecycle = conn.execute('SELECT lifecycle FROM memory').fetchone()[0]
            self.assertEqual(lifecycle, 'rejected')
            self.assertEqual(core.search(
                conn, 'proj-demo', 'agent-alice', 'Preferred editor is Helix'
            ), [])
            tombstones = conn.execute('SELECT COUNT(*) FROM tombstone').fetchone()[0]
            self.assertEqual(tombstones, 1)
        finally:
            conn.close()

    def test_builtin_memory_replace_supersedes_exact_mirrored_claim(self):
        p = self.provider()
        p.on_memory_write(
            'add', 'user', 'legacyhelix editor preference', {'tool_name': 'memory'}
        )
        p.on_memory_write(
            'replace', 'user', 'freshzed editor preference',
            {'tool_name': 'memory', 'old_text': 'legacyhelix editor preference'}
        )
        conn = store.open_store(self.db)
        try:
            event = conn.execute(
                "SELECT status, decision FROM ingest_event ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(event, ('processed', 'builtin_memory_replaced'))
            row = conn.execute(
                'SELECT m.lifecycle, v.content FROM memory m JOIN memory_version v '
                'ON v.id=m.current_version_id'
            ).fetchone()
            self.assertEqual(row, ('candidate', 'freshzed editor preference'))
            self.assertEqual(core.search(
                conn, 'proj-demo', 'agent-alice', 'legacyhelix'
            ), [])
            self.assertEqual(len(core.search(
                conn, 'proj-demo', 'agent-alice', 'freshzed'
            )), 1)
        finally:
            conn.close()
