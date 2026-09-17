"""MemCore plugin unit tests: binding, recall budget, tool schemas, tools.

Stdlib unittest. The tools run against a real temp MemCore store —
including the cross-profile loop (A remembers, B recalls) that is the
point of Phase 2.
"""
import importlib.util
import json
import pathlib
import sys
import tempfile
import threading
import unittest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT))

import plugin  # noqa: E402


def make_config(store_path=None, project='demo', agent=None, bindings=None,
                auto_join=False, budget=1200, max_items=8):
    memcore = {'default_project': project}
    if store_path:
        memcore['store_path'] = store_path
    if agent:
        memcore['agent_name'] = agent
    if bindings is not None:
        memcore['path_bindings'] = bindings
    if auto_join:
        memcore['auto_join'] = True
    memcore['inject'] = {'budget_chars': budget, 'max_items': max_items}
    return {'plugins': {'memcore': memcore}}


class TestProjectResolution(unittest.TestCase):

    def test_exact_match(self):
        b = [{'path': 'C:/work/memcore', 'project': 'memcore'}]
        self.assertEqual(plugin.resolve_project('C:/work/memcore', b), 'memcore')

    def test_nested_path_matches_longest_prefix(self):
        b = [{'path': 'C:/work', 'project': 'general'},
             {'path': 'C:/work/memcore', 'project': 'memcore'}]
        self.assertEqual(plugin.resolve_project('C:/work/memcore/src', b), 'memcore')

    def test_no_match_uses_default(self):
        b = [{'path': 'C:/work', 'project': 'general'}]
        self.assertEqual(plugin.resolve_project('D:/elsewhere', b, 'fallback'), 'fallback')

    def test_no_bindings_no_default_is_fail_closed(self):
        self.assertIsNone(plugin.resolve_project('C:/anywhere', [], None))
        self.assertIsNone(plugin.resolve_project('C:/anywhere', None, None))

    def test_case_and_separator_insensitive(self):
        b = [{'path': 'C:\\Work\\MemCore', 'project': 'memcore'}]
        self.assertEqual(plugin.resolve_project('c:/work/memcore/sub', b), 'memcore')

    def test_prefix_must_be_path_boundary(self):
        b = [{'path': 'C:/work/mem', 'project': 'mem'}]
        # 'memcore' starts with 'mem' but not 'mem/' — must NOT match
        self.assertIsNone(plugin.resolve_project('C:/work/memcore', b, None))


class TestBindingFromConfig(unittest.TestCase):

    def test_profile_name_is_agent_default(self):
        agent, project = plugin.binding_from_config(
            make_config(project='demo'), profile_name='sora')
        self.assertEqual(agent, 'sora')
        self.assertEqual(project, 'demo')

    def test_require_binding_raises_without_project(self):
        with self.assertRaises(plugin.ConfigError):
            plugin.require_binding({'plugins': {'memcore': {'agent_name': 'sora'}}})

    def test_require_binding_raises_without_any_config(self):
        with self.assertRaises(plugin.ConfigError):
            plugin.require_binding({}, profile_name='sora')

    def test_require_binding_ok(self):
        agent, project = plugin.require_binding(
            make_config(project='demo'), profile_name='sora')
        self.assertEqual((agent, project), ('sora', 'demo'))

    def test_store_path_expands_user_home_consistently(self):
        cfg = make_config(store_path='~/.memcore/test-expand.db')
        path = pathlib.Path(plugin.default_store_path(cfg))
        self.assertNotIn('~', str(path))
        self.assertTrue(str(path).startswith(str(pathlib.Path.home())))

    def test_binding_uses_session_scoped_runtime_cwd(self):
        from agent.runtime_cwd import set_session_cwd, clear_session_cwd
        with tempfile.TemporaryDirectory() as root:
            one = pathlib.Path(root) / 'one'
            two = pathlib.Path(root) / 'two'
            one.mkdir()
            two.mkdir()
            cfg = make_config(project=None, bindings=[
                {'path': str(one), 'project': 'one'},
                {'path': str(two), 'project': 'two'},
            ])
            try:
                set_session_cwd(str(one))
                self.assertEqual(plugin.binding_from_config(cfg, 'sora')[1], 'one')
                set_session_cwd(str(two))
                self.assertEqual(plugin.binding_from_config(cfg, 'sora')[1], 'two')
            finally:
                clear_session_cwd()


class TestProjectIdResolution(unittest.TestCase):

    def test_project_config_accepts_exact_project_id(self):
        with tempfile.TemporaryDirectory(prefix='memcore_project_id_') as root:
            db = str(pathlib.Path(root) / 'memory.db')
            conn = plugin.store.open_store(db)
            try:
                conn.execute("INSERT INTO project (id,name) VALUES ('uuid-project-123','demo')")
                self.assertEqual(
                    plugin._resolve_project_id(conn, 'uuid-project-123'),
                    'uuid-project-123'
                )
                self.assertEqual(plugin._resolve_project_id(conn, 'demo'), 'uuid-project-123')
            finally:
                conn.close()

    def test_ambiguous_project_name_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix='memcore_project_ambiguous_') as root:
            db = str(pathlib.Path(root) / 'memory.db')
            conn = plugin.store.open_store(db)
            try:
                conn.execute('DROP INDEX IF EXISTS idx_project_name_unique')
                conn.execute("INSERT INTO project (id,name) VALUES ('project-a','same-slug')")
                conn.execute("INSERT INTO project (id,name) VALUES ('project-b','same-slug')")
                with self.assertRaises(plugin.ConfigError):
                    plugin._resolve_project_id(conn, 'same-slug')
            finally:
                conn.close()


class TestRegistrationBridge(unittest.TestCase):

    def test_entrypoint_registers_only_native_memory_provider(self):
        init_path = PLUGIN_ROOT / '__init__.py'
        spec = importlib.util.spec_from_file_location(
            'memcore_registration_test', init_path,
            submodule_search_locations=[str(PLUGIN_ROOT)]
        )
        registration = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = registration
        try:
            spec.loader.exec_module(registration)

            class FakeCtx:
                def __init__(self):
                    self.provider = None
                    self.hooks = []
                    self.tools = []
                    self.llm = type(
                        'FakeLlm', (), {'complete_structured': lambda self, **kwargs: None}
                    )()
                def register_memory_provider(self, provider):
                    self.provider = provider
                def register_hook(self, *args, **kwargs):
                    self.hooks.append((args, kwargs))
                def register_tool(self, *args, **kwargs):
                    self.tools.append((args, kwargs))

            ctx = FakeCtx()
            registration.register(ctx)
            self.assertIsNotNone(ctx.provider)
            self.assertEqual(ctx.provider.name, 'memcore')
            self.assertIs(ctx.provider._plugin_llm, ctx.llm)
            self.assertEqual(ctx.hooks, [])
            self.assertEqual(ctx.tools, [])
            self.assertIn('memory_review_queue',
                          {s['name'] for s in ctx.provider.get_tool_schemas()})
        finally:
            sys.modules.pop(spec.name, None)


class TestRecallBlock(unittest.TestCase):

    def test_oversized_fact_is_skipped_without_losing_short_following_fact(self):
        rows = [self.row(1, 'x' * 400), self.row(2, 'Use SQLite only for local tests.')]
        block = plugin.build_recall_block([], rows, budget_chars=180)
        self.assertNotIn('xxx', block)
        self.assertIn('Use SQLite only for local tests.', block)
        self.assertEqual(block.count('- ['), 1)

    @staticmethod
    def row(i, content, scope='project'):
        return (f'mem-{i}', scope, 'accepted', 'source_backed', 'current', content)

    def test_empty_rows_give_empty_block(self):
        self.assertEqual(plugin.build_recall_block([], []), '')

    def test_pinned_first_then_hits_deduped(self):
        pinned = [self.row(1, 'pinned rule')]
        hits = [self.row(1, 'pinned rule'), self.row(2, 'searched fact')]
        block = plugin.build_recall_block(pinned, hits)
        self.assertIn('pinned rule', block)
        self.assertIn('searched fact', block)
        self.assertEqual(block.count('pinned rule'), 1)

    def test_budget_respected(self):
        rows = [self.row(i, 'x' * 200) for i in range(20)]
        block = plugin.build_recall_block([], rows, budget_chars=300, max_items=20)
        self.assertLessEqual(len(block), 300)

    def test_budget_includes_header_and_ellipsis(self):
        rows = [self.row(1, 'x' * 120), self.row(2, 'y' * 120)]
        block = plugin.build_recall_block([], rows, budget_chars=280, max_items=8)
        self.assertLessEqual(len(block), 280)

    def test_max_items_respected(self):
        rows = [self.row(i, f'fact {i}') for i in range(10)]
        block = plugin.build_recall_block([], rows, budget_chars=100000, max_items=3)
        self.assertEqual(block.count('- ['), 3)

    def test_whitespace_collapsed(self):
        block = plugin.build_recall_block([], [self.row(1, 'line1\n  line2\t tab')])
        self.assertIn('line1 line2 tab', block)

    def test_recall_block_exposes_trust_state(self):
        row = ('mem-candidate', 'project', 'candidate', 'unverified', 'current', 'pending claim')
        block = plugin.build_recall_block([], [row])
        self.assertIn('[project | candidate | unverified | current] pending claim', block)


class TestToolSchemas(unittest.TestCase):

    def test_six_tools_registered(self):
        names = [t['name'] for t in plugin.TOOL_SCHEMAS]
        self.assertEqual(names, [
            'memory_remember', 'memory_search', 'memory_promote',
            'memory_supersede', 'memory_reject', 'memory_feedback'])

    def test_no_identity_or_scope_params_in_schemas(self):
        forbidden = {'agent', 'agent_id', 'agent_name', 'project',
                     'project_id', 'scope', 'scope_hint'}
        for t in plugin.TOOL_SCHEMAS:
            props = set(t['parameters'].get('properties', {}))
            self.assertFalse(forbidden & props,
                             f'{t["name"]} leaks identity params: {forbidden & props}')

    def test_every_tool_has_a_handler(self):
        for t in plugin.TOOL_SCHEMAS:
            self.assertTrue(callable(t['handler']))


class ToolTestBase(unittest.TestCase):
    """Real temp store shared by the tool-integration tests."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_plugin_')
        self.store = str(pathlib.Path(self.tmp.name) / 'memory.db')
        self.config = make_config(store_path=self.store, project='demo')
        plugin.reset_conn()
        # bootstrap: agent + project + memberships via auto_join
        plugin.auto_join({'config': make_config(
            store_path=self.store, project='demo', agent='sora', auto_join=True),
            'profile_name': 'sora'})
        plugin.auto_join({'config': make_config(
            store_path=self.store, project='demo', agent='mika', auto_join=True),
            'profile_name': 'mika'})

    def tearDown(self):
        plugin.reset_conn()
        self.tmp.cleanup()

    def ctx(self, agent):
        return {'config': make_config(store_path=self.store, project='demo'),
                'profile_name': agent}


class TestToolsAgainstRealStore(ToolTestBase):

    def test_remember_then_cross_profile_recall(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'Deploy uses go build -ldflags -s -w'},
            self.ctx('sora')))
        self.assertTrue(out['success'])
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'deploy go build'}, self.ctx('mika')))
        self.assertTrue(seen['success'])
        self.assertEqual(len(seen['results']), 1)
        self.assertIn('go build', seen['results'][0]['content'])
        self.assertEqual(seen['results'][0]['freshness'], 'current')

    def test_remember_scope_cannot_be_model_overridden(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'shared deployment preference', 'scope_hint': 'private'},
            self.ctx('sora')))
        self.assertTrue(out['success'])
        self.assertEqual(out['scope'], 'project')
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'shared deployment preference'}, self.ctx('mika')))
        self.assertEqual(len(seen['results']), 1)
        self.assertEqual(seen['results'][0]['scope'], 'project')

    def test_tool_calls_work_across_threads(self):
        created = json.loads(plugin.tool_memory_remember(
            {'content': 'thread local sqlite connection proof'}, self.ctx('sora')))
        self.assertTrue(created['success'])
        results = []
        thread = threading.Thread(target=lambda: results.append(json.loads(
            plugin.tool_memory_search({'query': 'thread local sqlite'}, self.ctx('sora'))
        )))
        thread.start()
        thread.join()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]['success'], results[0])
        self.assertEqual(len(results[0]['results']), 1)

    def test_feedback_rolls_back_if_audit_insert_fails(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'atomic feedback candidate'}, self.ctx('sora')))
        mem_id = out['memory_id']
        original = plugin.store_audit
        plugin.store_audit = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('audit fail'))
        try:
            result = json.loads(plugin.tool_memory_feedback(
                {'memory_id': mem_id, 'outcome': 'accepted'}, self.ctx('sora')))
        finally:
            plugin.store_audit = original
        self.assertFalse(result['success'])
        conn = plugin._get_conn(self.store)
        lifecycle = conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'candidate')

    def test_feedback_audit_uses_iso_z_timestamp(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'feedback audit iso timestamp'}, self.ctx('sora')))
        mem_id = out['memory_id']
        result = json.loads(plugin.tool_memory_feedback(
            {'memory_id': mem_id, 'outcome': 'stale'}, self.ctx('sora')))
        self.assertTrue(result['success'], result)
        conn = plugin._get_conn(self.store)
        created_at = conn.execute(
            "SELECT created_at FROM audit_event WHERE memory_id=? AND action='feedback' "
            'ORDER BY id DESC LIMIT 1', (mem_id,)
        ).fetchone()[0]
        self.assertTrue(created_at.endswith('Z'), created_at)

    def test_feedback_cannot_resurrect_rejected_memory(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'terminal rejected feedback claim'}, self.ctx('sora')))
        mem_id = out['memory_id']
        plugin.tool_memory_reject(
            {'memory_id': mem_id, 'reason': 'wrong'}, self.ctx('sora'))
        accepted = json.loads(plugin.tool_memory_feedback(
            {'memory_id': mem_id, 'outcome': 'accepted'}, self.ctx('sora')))
        self.assertFalse(accepted['success'])
        conn = plugin._get_conn(self.store)
        lifecycle = conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'rejected')

    def test_feedback_accept_blocked_by_tombstone_from_duplicate_claim(self):
        content = 'duplicate candidate later tombstoned'
        first = json.loads(plugin.tool_memory_remember(
            {'content': content}, self.ctx('sora')))
        second = json.loads(plugin.tool_memory_remember(
            {'content': content}, self.ctx('mika')))
        self.assertNotEqual(first['memory_id'], second['memory_id'])
        plugin.tool_memory_reject(
            {'memory_id': second['memory_id'], 'reason': 'wrong'}, self.ctx('mika'))
        accepted = json.loads(plugin.tool_memory_feedback(
            {'memory_id': first['memory_id'], 'outcome': 'accepted'}, self.ctx('sora')))
        self.assertFalse(accepted['success'])
        self.assertIn('TombstoneBlocked', accepted['error'])
        conn = plugin._get_conn(self.store)
        lifecycle = conn.execute(
            'SELECT lifecycle FROM memory WHERE id=?', (first['memory_id'],)
        ).fetchone()[0]
        self.assertEqual(lifecycle, 'candidate')

    def test_stale_feedback_does_not_mutate_terminal_memory(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'terminal stale feedback claim'}, self.ctx('sora')))
        mem_id = out['memory_id']
        plugin.tool_memory_reject(
            {'memory_id': mem_id, 'reason': 'wrong'}, self.ctx('sora'))
        conn = plugin._get_conn(self.store)
        before = conn.execute(
            'SELECT freshness, updated_at FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        result = json.loads(plugin.tool_memory_feedback(
            {'memory_id': mem_id, 'outcome': 'stale'}, self.ctx('sora')))
        self.assertFalse(result['success'])
        after = conn.execute(
            'SELECT freshness, updated_at FROM memory WHERE id=?', (mem_id,)
        ).fetchone()
        self.assertEqual(after, before)

    def test_feedback_accept_respects_owner_private_tombstone(self):
        conn = plugin._get_conn(self.store)
        content = 'sora private duplicate later rejected'
        first, _ = plugin.core.create_memory(
            conn, 'proj-demo', 'agent-sora', content, scope='private'
        )
        second, _ = plugin.core.create_memory(
            conn, 'proj-demo', 'agent-sora', content, scope='private'
        )
        plugin.core.reject(conn, second, 'agent-sora', 'private wrong claim')
        accepted = json.loads(plugin.tool_memory_feedback(
            {'memory_id': first, 'outcome': 'accepted'}, self.ctx('sora')))
        self.assertFalse(accepted['success'])
        self.assertIn('TombstoneBlocked', accepted['error'])
        self.assertEqual(
            conn.execute('SELECT lifecycle FROM memory WHERE id=?', (first,)).fetchone()[0],
            'candidate'
        )

    def test_reject_tombstones_the_claim(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'the port is 4173'},
            self.ctx('sora')))
        mem_id = out['memory_id']
        rej = json.loads(plugin.tool_memory_reject(
            {'memory_id': mem_id, 'reason': 'wrong port'}, self.ctx('sora')))
        self.assertTrue(rej['success'])
        again = json.loads(plugin.tool_memory_remember(
            {'content': 'the port is 4173'},
            self.ctx('mika')))
        self.assertFalse(again['success'])
        self.assertIn('tombstone', again['error'].lower())

    def test_supersede_keeps_history(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'port is 4173'}, self.ctx('sora')))
        sup = json.loads(plugin.tool_memory_supersede(
            {'memory_id': out['memory_id'], 'new_content': 'port is 4890',
             'reason': 'port migrated'}, self.ctx('sora')))
        self.assertTrue(sup['success'])
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'port is 4890'}, self.ctx('mika')))
        self.assertEqual(len(seen['results']), 1)

    def test_feedback_accept_updates_lifecycle(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'wAL mode is required'},
            self.ctx('sora')))
        fb = json.loads(plugin.tool_memory_feedback(
            {'memory_id': out['memory_id'], 'outcome': 'accepted'}, self.ctx('sora')))
        self.assertTrue(fb.get('success'), msg=f"feedback failed: {fb}")
        self.assertEqual(fb.get('outcome'), 'accepted')

    def test_fail_closed_without_membership(self):
        conn = plugin._get_conn(self.store)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-locked', 'locked')")
        conn.commit()
        locked = {'config': make_config(store_path=self.store, project='locked'),
                  'profile_name': 'sora'}
        remembered = json.loads(plugin.tool_memory_remember(
            {'content': 'must not bypass membership'}, locked))
        searched = json.loads(plugin.tool_memory_search({'query': 'anything'}, locked))
        self.assertFalse(remembered['success'])
        self.assertFalse(searched['success'])
        self.assertIn('not a member', remembered['error'])
        self.assertIn('not a member', searched['error'])

    def test_mutation_cannot_cross_bound_project(self):
        other_ctx = {'config': make_config(store_path=self.store, project='other',
                                            agent='sora', auto_join=True),
                     'profile_name': 'sora'}
        plugin.auto_join(other_ctx)
        other_mem = json.loads(plugin.tool_memory_remember(
            {'content': 'other project fact'}, other_ctx))['memory_id']
        rejected = json.loads(plugin.tool_memory_reject(
            {'memory_id': other_mem, 'reason': 'cross-project attempt'}, self.ctx('sora')))
        superseded = json.loads(plugin.tool_memory_supersede(
            {'memory_id': other_mem, 'new_content': 'cross-project rewrite'}, self.ctx('sora')))
        self.assertFalse(rejected['success'])
        self.assertFalse(superseded['success'])
        conn = plugin._get_conn(self.store)
        row = conn.execute('SELECT lifecycle FROM memory WHERE id=?', (other_mem,)).fetchone()
        self.assertEqual(row[0], 'candidate')

    def test_auto_join_audited_once(self):
        cfg = {'config': make_config(store_path=self.store, project='auditjoin',
                                     agent='sora', auto_join=True),
               'profile_name': 'sora'}
        plugin.auto_join(cfg)
        plugin.auto_join(cfg)
        conn = plugin._get_conn(self.store)
        n = conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='agent_joined' "
            "AND project_id='proj-auditjoin' AND actor_agent_id='agent-sora'"
        ).fetchone()[0]
        self.assertEqual(n, 1)

    def test_auto_join_uses_existing_exact_project_uuid(self):
        project_uuid = '12345678-1234-5678-1234-567812345678'
        conn = plugin._get_conn(self.store)
        conn.execute('INSERT INTO project (id,name) VALUES (?,?)', (project_uuid, 'uuid-project'))
        cfg = {'config': make_config(
            store_path=self.store, project=project_uuid,
            agent='uuidagent', auto_join=True),
            'profile_name': 'uuidagent'}
        plugin.auto_join(cfg)
        self.assertIsNotNone(conn.execute(
            'SELECT 1 FROM project_membership WHERE project_id=? AND agent_id=?',
            (project_uuid, 'agent-uuidagent')
        ).fetchone())
        self.assertIsNone(conn.execute(
            'SELECT 1 FROM project WHERE id=?', ('proj-' + project_uuid,)
        ).fetchone())

    def test_auto_join_missing_project_uuid_fails_closed_without_artifacts(self):
        project_uuid = '87654321-4321-8765-4321-876543218765'
        cfg = {'config': make_config(
            store_path=self.store, project=project_uuid,
            agent='missinguuid', auto_join=True),
            'profile_name': 'missinguuid'}
        plugin.auto_join(cfg)
        conn = plugin._get_conn(self.store)
        self.assertIsNone(conn.execute(
            'SELECT 1 FROM agent WHERE id=?', ('agent-missinguuid',)
        ).fetchone())
        self.assertIsNone(conn.execute(
            'SELECT 1 FROM project WHERE id IN (?,?)',
            (project_uuid, 'proj-' + project_uuid)
        ).fetchone())

    def test_auto_join_fails_closed_on_agent_identity_collision(self):
        conn = plugin._get_conn(self.store)
        conn.execute(
            "INSERT INTO agent (id,name,profile_key) "
            "VALUES ('agent-collision','different','different')"
        )
        cfg = {'config': make_config(
            store_path=self.store, project='collision-project',
            agent='collision', auto_join=True),
            'profile_name': 'collision'}
        plugin.auto_join(cfg)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM project WHERE id='proj-collision-project'"
        ).fetchone())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM project_membership WHERE agent_id='agent-collision'"
        ).fetchone())
        self.assertEqual(
            conn.execute(
                "SELECT name,profile_key FROM agent WHERE id='agent-collision'"
            ).fetchone(),
            ('different', 'different')
        )

    def test_auto_join_rolls_back_if_audit_fails(self):
        cfg = {'config': make_config(store_path=self.store, project='rollbackjoin',
                                     agent='newbie', auto_join=True),
               'profile_name': 'newbie'}
        original = plugin.store_audit
        plugin.store_audit = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('audit fail'))
        try:
            plugin.auto_join(cfg)
        finally:
            plugin.store_audit = original
        conn = plugin._get_conn(self.store)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM project_membership WHERE project_id='proj-rollbackjoin' "
            "AND agent_id='agent-newbie'"
        ).fetchone())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM project WHERE id='proj-rollbackjoin'"
        ).fetchone())
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM agent WHERE id='agent-newbie'"
        ).fetchone())

    def test_fail_closed_without_project(self):
        no_project = {'config': {'plugins': {'memcore': {'store_path': self.store}}}}
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'orphan thought'}, no_project))
        self.assertFalse(out['success'])
        self.assertIn('binding', out['error'].lower())

    def test_missing_store_is_a_clear_error(self):
        cfg = make_config(store_path=str(pathlib.Path(self.tmp.name) / 'nope.db'),
                          project='demo')
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'x'}, {'config': cfg, 'profile_name': 'sora'}))
        self.assertFalse(out['success'])
        self.assertIn('init', out['error'])

    def test_existing_but_incompatible_store_is_not_reported_missing(self):
        future = str(pathlib.Path(self.tmp.name) / 'future.db')
        conn = plugin.store.open_store(future)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) "
            "VALUES ('9999_future', '9999-01-01 00:00:00')"
        )
        conn.close()
        cfg = make_config(store_path=future, project='demo')
        out = json.loads(plugin.tool_memory_search(
            {'query': 'anything'}, {'config': cfg, 'profile_name': 'sora'}))
        self.assertFalse(out['success'])
        self.assertIn('store open failed', out['error'])
        self.assertIn('invalid migration history', out['error'])
        self.assertIn('9999_future', out['error'])
        self.assertNotIn('run: python -m memcore', out['error'])


class TestHooks(ToolTestBase):

    def test_pre_llm_call_injects_shared_memory(self):
        plugin.tool_memory_remember(
            {'content': 'novelclaw runs on port 4890'},
            self.ctx('sora'))
        block = plugin.pre_llm_call({'config': make_config(store_path=self.store),
                                     'profile_name': 'mika',
                                     'user_message': 'what port does novelclaw use?'})
        self.assertIsNotNone(block)
        self.assertIn('4890', block['context'])

    def test_pre_llm_call_empty_store_returns_none(self):
        block = plugin.pre_llm_call({'config': make_config(store_path=self.store),
                                     'profile_name': 'mika',
                                     'user_message': 'anything'})
        self.assertIsNone(block)

    def test_pre_llm_call_fail_closed_without_binding(self):
        block = plugin.pre_llm_call({'config': {'plugins': {}},
                                     'profile_name': 'mika',
                                     'user_message': 'hello'})
        self.assertIsNone(block)

    def test_pre_llm_call_pinned_rows_are_bounded_and_ordered(self):
        ids = []
        for text in ('pin oldest', 'pin newest', 'pin critical', 'pin overflow'):
            out = json.loads(plugin.tool_memory_remember({'content': text}, self.ctx('sora')))
            ids.append(out['memory_id'])
        conn = plugin._get_conn(self.store)
        conn.execute("UPDATE memory SET pinned=1, updated_at='2026-01-01T00:00:00Z' WHERE id=?", (ids[0],))
        conn.execute("UPDATE memory SET pinned=1, critical=1, updated_at='2026-01-01T12:00:00Z' WHERE id=?", (ids[1],))
        conn.execute("UPDATE memory SET pinned=1, critical=1, updated_at='2026-01-02T00:00:00Z' WHERE id=?", (ids[2],))
        conn.execute("UPDATE memory SET pinned=1, updated_at='2026-01-03T00:00:00Z' WHERE id=?", (ids[3],))
        block = plugin.pre_llm_call({
            'config': make_config(store_path=self.store, max_items=2),
            'profile_name': 'mika', 'user_message': ''
        })
        self.assertIsNotNone(block)
        self.assertEqual(block['context'].count('- ['), 2)
        self.assertIn('pin critical', block['context'])
        self.assertIn('pin newest', block['context'])
        self.assertNotIn('pin overflow', block['context'])
        self.assertNotIn('pin oldest', block['context'])

    def test_pre_llm_call_negative_limits_fail_closed(self):
        block = plugin.pre_llm_call({
            'config': make_config(store_path=self.store, budget=-1, max_items=-1),
            'profile_name': 'mika', 'user_message': 'anything'
        })
        self.assertIsNone(block)

    def test_is_trivial_query_matches_journal_definition(self):
        for q in ('สวัสดีค่ะ', 'ดี', 'ok', 'ขอบคุณครับ'):
            self.assertTrue(plugin._is_trivial_query(q), q)
        for q in ('มาดู memcore หน่อย', 'what port does novelclaw use?',
                  '', 'ok, deploy this now'):
            self.assertFalse(plugin._is_trivial_query(q), q)

    def test_hook_excludes_unrelated_noncritical_pins(self):
        plugin.tool_memory_remember({'content': 'unrelated pinned fact'}, self.ctx('sora'))
        conn = plugin._get_conn(self.store)
        conn.execute('UPDATE memory SET pinned=1')
        block = plugin.pre_llm_call({'config': make_config(store_path=self.store),
                                     'profile_name': 'mika',
                                     'user_message': 'absentquery'})
        self.assertIsNone(block)
        conn.execute('UPDATE memory SET critical=1')
        block = plugin.pre_llm_call({'config': make_config(store_path=self.store),
                                     'profile_name': 'mika',
                                     'user_message': 'absentquery'})
        self.assertIn('unrelated pinned fact', block['context'])

    def test_pre_llm_call_skips_trivial_greeting(self):
        plugin.tool_memory_remember(
            {'content': 'novelclaw runs on port 4890'},
            self.ctx('sora'))
        block = plugin.pre_llm_call({'config': make_config(store_path=self.store),
                                     'profile_name': 'mika',
                                     'user_message': 'สวัสดีค่ะ'})
        self.assertIsNone(block)

    def test_pre_llm_call_keeps_substantive_thai_query(self):
        plugin.tool_memory_remember(
            {'content': 'memcore test marker runs on port 4890'},
            self.ctx('sora'))
        block = plugin.pre_llm_call({'config': make_config(store_path=self.store),
                                     'profile_name': 'mika',
                                     'user_message': 'มาดู memcore หน่อย'})
        self.assertIsNotNone(block)
        self.assertIn('4890', block['context'])

    def test_post_llm_call_records_candidate_private(self):
        long_note = ('Observed that the deploy pipeline retried twice before '
                     'succeeding and the second attempt used the cached layer.')
        out = plugin.post_llm_call({'config': make_config(store_path=self.store),
                                    'profile_name': 'mika',
                                    'assistant_message': long_note})
        self.assertIsNone(out)  # hooks return None; effect is in the store
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'deploy pipeline retried'}, self.ctx('mika')))
        self.assertEqual(len(seen['results']), 1)
        self.assertEqual(seen['results'][0]['scope'], 'private')
        self.assertEqual(seen['results'][0]['lifecycle'], 'candidate')

    def test_post_llm_call_replay_is_idempotent(self):
        long_note = (
            'Observed that the deploy pipeline retried twice before succeeding '
            'and the second attempt used the cached layer for repeat-hook proof.'
        )
        ctx = {'config': make_config(store_path=self.store),
               'profile_name': 'mika', 'assistant_message': long_note}
        plugin.post_llm_call(ctx)
        plugin.post_llm_call(ctx)
        conn = plugin._get_conn(self.store)
        rows = conn.execute(
            'SELECT COUNT(*) FROM memory m JOIN memory_version v '
            'ON v.id=m.current_version_id WHERE m.owner_agent_id=? '
            "AND m.scope='private' AND v.content=?",
            ('agent-mika', long_note)
        ).fetchone()[0]
        self.assertEqual(rows, 1)
        keys = conn.execute(
            "SELECT COUNT(*) FROM idempotency_key WHERE key LIKE 'observe:%'"
        ).fetchone()[0]
        self.assertEqual(keys, 1)

    def test_post_llm_call_ignores_short_messages(self):
        out = plugin.post_llm_call({'config': make_config(store_path=self.store),
                                    'profile_name': 'mika',
                                    'assistant_message': 'too short'})
        self.assertIsNone(out)


if __name__ == '__main__':
    unittest.main()
