"""CLI regression tests for safe import preview."""
import contextlib
import io
import json
import os
import pathlib
import shutil
import sqlite3
import tempfile
import unittest

from memcore import __main__ as cli
from memcore import core, store


class CliImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_cli_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        self.batch = os.path.join(self.tmp.name, 'batch.json')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo', 'demo')")
        conn.commit()
        conn.close()
        with open(self.batch, 'w', encoding='utf-8') as f:
            json.dump([
                {'summary': 'safe import claim', 'type': 'fact'},
                {'summary': 'safe import claim', 'type': 'fact'},
                {'summary': '   ', 'type': 'fact'},
                {'summary': 'bad evidence', 'evidence': 'not-a-list'},
            ], f)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_stdio_reconfigures_utf8_when_supported(self):
        class FakeStream:
            def __init__(self):
                self.calls = []
            def reconfigure(self, **kwargs):
                self.calls.append(kwargs)

        old_out, old_err = cli.sys.stdout, cli.sys.stderr
        fake_out, fake_err = FakeStream(), FakeStream()
        try:
            cli.sys.stdout, cli.sys.stderr = fake_out, fake_err
            cli._configure_stdio_utf8()
        finally:
            cli.sys.stdout, cli.sys.stderr = old_out, old_err
        self.assertEqual(fake_out.calls[-1], {'encoding': 'utf-8', 'errors': 'replace'})
        self.assertEqual(fake_err.calls[-1], {'encoding': 'utf-8', 'errors': 'replace'})

    def test_import_dry_run_performs_zero_domain_writes(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'import', '--file', self.batch,
                '--agent', 'previewer', '--project', 'demo', '--dry-run'
            ])
        text = output.getvalue()
        self.assertIn('would add: 1', text)
        self.assertIn('duplicate_input: 1', text)
        self.assertIn('empty_summary: 1', text)
        self.assertIn('invalid_evidence: 1', text)
        self.assertIn('writes performed: 0', text)

        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM memory').fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent WHERE id='agent-previewer'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM project_membership WHERE agent_id='agent-previewer'"
            ).fetchone()[0], 0)
        finally:
            conn.close()

    def test_import_setup_rolls_back_if_join_audit_fails(self):
        original = core._audit
        core._audit = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError('audit fail'))
        try:
            with self.assertRaises(RuntimeError):
                cli.main([
                    '--db', self.db, 'import', '--file', self.batch,
                    '--agent', 'brokenjoin', '--project', 'demo'
                ])
        finally:
            core._audit = original
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent WHERE id='agent-brokenjoin'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM project_membership WHERE agent_id='agent-brokenjoin'"
            ).fetchone()[0], 0)
        finally:
            conn.close()

    def test_dry_run_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing.db')
        output = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(output):
            cli.main([
                '--db', missing, 'import', '--file', self.batch,
                '--agent', 'previewer', '--project', 'demo', '--dry-run'
            ])
        self.assertFalse(os.path.exists(missing))

    def test_gc_dry_run_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing-gc.db')
        with self.assertRaises(SystemExit):
            cli.main(['--db', missing, 'gc'])
        self.assertFalse(os.path.exists(missing))

    def test_doctor_missing_store_does_not_create_database(self):
        missing = os.path.join(self.tmp.name, 'missing-doctor.db')
        with self.assertRaises(SystemExit):
            cli.main(['--db', missing, 'doctor'])
        self.assertFalse(os.path.exists(missing))

    def _deploy_module(self):
        repo_root = pathlib.Path(cli.__file__).resolve().parents[1]
        return cli._load_deploy_module(repo_root / 'scripts' / 'deploy_hermes_plugin.py')

    def _stage_plugin_deployment(self, mutate=None, target=None):
        deploy = self._deploy_module()
        if target is None:
            target = pathlib.Path(self.tmp.name) / 'plugins' / 'memcore'
        target.mkdir(parents=True, exist_ok=True)
        for relative in deploy.RUNTIME_FILES:
            destination = target / pathlib.PurePosixPath(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(
                deploy.SOURCE_ROOT / pathlib.PurePosixPath(relative), destination
            )
        if mutate:
            mutate(target)
        return target

    def _run_doctor_with_plugin_env(self, target):
        # Point HERMES_HOME at a stub home whose config.yaml does not enable
        # memcore — discovery picks the first root with a config.yaml, so an
        # empty dir would fall through to the real LOCALAPPDATA Hermes config.
        hermes_home = pathlib.Path(self.tmp.name) / 'stub-hermes-home'
        hermes_home.mkdir(exist_ok=True)
        (hermes_home / 'config.yaml').write_text(
            'plugins:\n  enabled: []\n', encoding='utf-8'
        )
        saved = {
            key: os.environ.get(key)
            for key in ('HERMES_HOME', 'MEMCORE_HERMES_PLUGIN_DIR')
        }
        os.environ['MEMCORE_HERMES_PLUGIN_DIR'] = str(target)
        os.environ['HERMES_HOME'] = str(hermes_home)
        # doctor gates on recovery readiness; these tests are about plugin
        # deployment, so give the fixture store a snapshot to keep that gate
        # from masking the deployment signal.
        store.backup_store(self.db)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
            code = None
        except SystemExit as exc:
            code = exc.code
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, output.getvalue()

    def test_doctor_reports_plugin_deployment_in_sync(self):
        target = self._stage_plugin_deployment()
        code, output = self._run_doctor_with_plugin_env(target)
        self.assertIsNone(code)
        self.assertIn('plugin deploy check: OK', output)

    def test_doctor_fails_on_plugin_deployment_drift(self):
        def mutate(target):
            plugin_yaml = target / 'plugin.yaml'
            plugin_yaml.write_text(
                plugin_yaml.read_text(encoding='utf-8') + '\n# drift\n',
                encoding='utf-8',
            )
        target = self._stage_plugin_deployment(mutate)
        code, output = self._run_doctor_with_plugin_env(target)
        self.assertEqual(code, 1)
        self.assertIn('plugin deploy check: OUT OF SYNC - plugin.yaml', output)

    def test_doctor_fails_when_plugin_not_installed(self):
        code, output = self._run_doctor_with_plugin_env(
            pathlib.Path(self.tmp.name) / 'plugins' / 'absent-memcore'
        )
        self.assertEqual(code, 1)
        self.assertIn('plugin deploy check: NOT INSTALLED', output)

    def test_read_commands_do_not_create_missing_store(self):
        cases = [
            ['stats'],
            ['project', 'list'],
            ['search', '--project', 'demo', '--agent', 'previewer', 'anything'],
        ]
        for index, command in enumerate(cases):
            missing = os.path.join(self.tmp.name, f'missing-read-{index}.db')
            with self.subTest(command=command), self.assertRaises(SystemExit):
                cli.main(['--db', missing] + command)
            self.assertFalse(os.path.exists(missing))

    def test_agent_add_does_not_false_succeed_on_profile_key_collision(self):
        conn = store.open_store(self.db)
        conn.execute(
            "INSERT INTO agent (id,name,profile_key) VALUES ('agent-existing','existing','taken')"
        )
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            cli.main(['--db', self.db, 'agent', 'add', 'taken'])
        self.assertIn('profile_key taken already belongs', str(cm.exception))
        conn = store.open_store(self.db)
        try:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM agent WHERE id='agent-taken'").fetchone()
            )
        finally:
            conn.close()

    def test_agent_add_idempotent_only_for_same_identity(self):
        cli.main(['--db', self.db, 'agent', 'add', 'same'])
        cli.main(['--db', self.db, 'agent', 'add', 'same'])
        conn = store.open_store(self.db)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM agent WHERE id='agent-same'").fetchone()[0],
                1
            )
        finally:
            conn.close()

    def test_cli_project_references_accept_exact_project_id(self):
        project_id = '12345678-1234-5678-1234-567812345678'
        conn = store.open_store(self.db)
        conn.execute('INSERT INTO project (id,name) VALUES (?,?)', (project_id, 'uuid-demo'))
        conn.close()
        cli.main(['--db', self.db, 'agent', 'add', 'uuiduser'])
        cli.main(['--db', self.db, 'member', 'add', project_id, 'uuiduser'])
        cli.main([
            '--db', self.db, 'remember', '--project', project_id,
            '--agent', 'uuiduser', '--scope', 'project', 'uuid backed fact'
        ])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.main([
                '--db', self.db, 'search', '--project', project_id,
                '--agent', 'uuiduser', 'uuid backed'
            ])
        self.assertIn('uuid backed fact', output.getvalue())

    def test_project_add_rejects_duplicate_name_owned_by_other_id(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id,name) VALUES ('uuid-existing','same-name')")
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            cli.main(['--db', self.db, 'project', 'add', 'same-name'])
        self.assertIn('already belongs to uuid-existing', str(cm.exception))
        conn = store.open_store(self.db)
        try:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM project WHERE id='proj-same-name'").fetchone()
            )
        finally:
            conn.close()

    def test_import_dry_run_rejects_agent_id_identity_collision(self):
        conn = store.open_store(self.db)
        conn.execute(
            "INSERT INTO agent (id,name,profile_key) "
            "VALUES ('agent-previewer','different','different')"
        )
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            cli.main([
                '--db', self.db, 'import', '--file', self.batch,
                '--agent', 'previewer', '--project', 'demo', '--dry-run'
            ])
        self.assertIn('agent id agent-previewer has different identity', str(cm.exception))

    def test_member_add_missing_agent_is_clear_and_releases_store(self):
        with self.assertRaises(SystemExit) as cm:
            cli.main(['--db', self.db, 'member', 'add', 'demo', 'ghost'])
        self.assertIn('agent agent-ghost does not exist', str(cm.exception))
        moved = self.db + '.moved'
        os.replace(self.db, moved)
        os.replace(moved, self.db)

    def test_member_add_does_not_claim_role_change_it_did_not_apply(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-a','a','a')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-a','member')"
        )
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            cli.main([
                '--db', self.db, 'member', 'add', 'demo', 'a', '--role', 'owner'
            ])
        self.assertIn('already exists with role member', str(cm.exception))
        conn = store.open_store(self.db)
        try:
            role = conn.execute(
                "SELECT role FROM project_membership WHERE project_id='proj-demo' "
                "AND agent_id='agent-a'"
            ).fetchone()[0]
            self.assertEqual(role, 'member')
        finally:
            conn.close()

    def test_failed_memory_write_command_releases_store(self):
        with self.assertRaises(SystemExit):
            cli.main([
                '--db', self.db, 'remember', '--project', 'demo',
                '--agent', 'ghost', 'must fail closed'
            ])
        moved = self.db + '.moved'
        os.replace(self.db, moved)
        os.replace(moved, self.db)

    def test_cli_tombstone_override_requires_owner_and_audits(self):
        conn = store.open_store(self.db)
        for name, role in (( 'owner', 'owner'), ('member', 'member')):
            aid = 'agent-' + name
            conn.execute('INSERT INTO agent (id,name,profile_key) VALUES (?,?,?)', (aid,name,name))
            conn.execute(
                'INSERT INTO project_membership (project_id,agent_id,role) VALUES (?,?,?)',
                ('proj-demo', aid, role)
            )
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-owner', 'cli tombstone override claim', scope='project'
        )
        core.reject(conn, mem_id, 'agent-owner', 'wrong')
        tomb_id = conn.execute('SELECT id FROM tombstone').fetchone()[0]
        conn.close()
        with self.assertRaises(SystemExit):
            cli.main(['--db', self.db, 'tombstone', 'override', tomb_id, '--agent', 'member'])
        cli.main(['--db', self.db, 'tombstone', 'override', tomb_id, '--agent', 'owner'])
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                'SELECT overridden_by FROM tombstone WHERE id=?', (tomb_id,)
            ).fetchone()[0], 'agent-owner')
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM audit_event WHERE action='tombstone_override' AND project_id='proj-demo'"
            ).fetchone())
        finally:
            conn.close()

    def test_cli_restore_roundtrips_disabled_memory(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-owner','owner','owner')")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-owner','owner',datetime('now'))")
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-owner', 'cli reversible accepted memory', scope='project'
        )
        conn.execute("UPDATE memory SET lifecycle='accepted' WHERE id=?", (mem_id,))
        core.deactivate(conn, mem_id, 'agent-owner')
        conn.close()
        cli.main(['--db', self.db, 'restore', mem_id, '--agent', 'owner'])
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()[0], 'accepted')
        finally:
            conn.close()

    def test_mutation_cli_rejects_agent_identity_collision(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-spoof','different','different')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-owner','owner','owner')")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-spoof','member',datetime('now'))")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-owner','owner',datetime('now'))")
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-owner', 'identity collision mutation target', scope='project'
        )
        conn.close()
        with self.assertRaises(SystemExit) as cm:
            cli.main(['--db', self.db, 'reject', mem_id, '--agent', 'spoof', 'should fail'])
        self.assertIn('different identity', str(cm.exception))
        conn = store.open_store(self.db)
        try:
            self.assertEqual(conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()[0], 'candidate')
        finally:
            conn.close()

    def test_doctor_detects_orphaned_idempotency_rows(self):
        conn = store.open_store(self.db)
        conn.execute(
            "INSERT INTO idempotency_key (key,project_id,memory_id,version_id) "
            "VALUES ('broken','proj-demo','mem-missing','ver-missing')"
        )
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('idempotency violations: 1', output.getvalue())

    def test_doctor_detects_cross_memory_current_version_pointer(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-pointer','pointer','pointer')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-pointer','owner')"
        )
        first_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-pointer', 'first pointer claim', scope='project'
        )
        _second_id, second_ver = core.create_memory(
            conn, 'proj-demo', 'agent-pointer', 'second pointer claim', scope='project'
        )
        conn.execute('DROP TRIGGER memory_current_version_owner_update')
        conn.execute(
            'UPDATE memory SET current_version_id=?, claim_fingerprint=? WHERE id=?',
            (second_ver, core.fingerprint('second pointer claim'), first_id)
        )
        self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('current-version ownership violations:', output.getvalue())
        self.assertIn(first_id, output.getvalue())

    def test_doctor_detects_current_memory_fingerprint_drift(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-fp','fp','fp')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-fp','owner')"
        )
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-fp', 'fingerprint drift sentinel', scope='project'
        )
        conn.execute(
            "UPDATE memory SET claim_fingerprint='0000000000000000' WHERE id=?",
            (mem_id,)
        )
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('memory fingerprint violations:', output.getvalue())
        self.assertIn(mem_id, output.getvalue())

    def test_doctor_detects_rejected_memory_without_active_guard(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-a','a','a')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-a','member')"
        )
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-a', 'unguarded rejected claim', scope='project'
        )
        conn.execute("UPDATE memory SET lifecycle='rejected' WHERE id=?", (mem_id,))
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn(f"unguarded rejected memories: ['{mem_id}']", output.getvalue())

    def test_doctor_detects_crash_visible_migration_lock(self):
        conn = store.open_store(self.db)
        conn.execute(
            'INSERT INTO schema_migrations '
            '(version, applied_at, lock_holder, lock_until) '
            "VALUES (?, datetime('now'), 'crashed-worker', 0)",
            (store._MIGRATION_LOCK_VERSION,)
        )
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('crashed-worker', output.getvalue())

    def _write_hermes_config(self, agent, project='demo'):
        home = os.path.join(self.tmp.name, 'hermes-home')
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, 'config.yaml'), 'w', encoding='utf-8') as f:
            f.write(
                'plugins:\n  enabled: [memcore]\n  entries:\n    memcore:\n'
                '      settings:\n'
                f'        agent_name: {agent}\n'
                f'        default_project: {project}\n'
                f'        store_path: {json.dumps(self.db)}\n'
            )
        self._stage_plugin_deployment(target=pathlib.Path(home) / 'plugins' / 'memcore')
        return home

    def test_doctor_accepts_matching_enabled_profile_binding(self):
        home = self._write_hermes_config('checker')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-checker','checker','checker')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-checker','member')"
        )
        conn.close()
        # doctor gates on recovery readiness; give the fixture one so the
        # assertion under test (binding check) is not masked by a missing backup.
        store.backup_store(self.db)
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn('default: OK', output.getvalue())

    def test_doctor_accepts_exact_project_id_binding(self):
        home = self._write_hermes_config('checker', project='uuid-project-123')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id,name) VALUES ('uuid-project-123','renamed-project')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-checker','checker','checker')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('uuid-project-123','agent-checker','member')"
        )
        conn.close()
        # doctor gates on recovery readiness; without a snapshot the binding
        # assertion below would be masked by an unrelated backup failure.
        store.backup_store(self.db)
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn('default: OK', output.getvalue())

    def test_doctor_fails_closed_on_ambiguous_project_slug(self):
        home = self._write_hermes_config('checker', project='same-slug')
        conn = store.open_store(self.db)
        conn.execute('DROP INDEX IF EXISTS idx_project_name_unique')
        conn.execute("INSERT INTO project (id,name) VALUES ('project-a','same-slug')")
        conn.execute("INSERT INTO project (id,name) VALUES ('project-b','same-slug')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-checker','checker','checker')")
        conn.close()
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn('ambiguous_project:same-slug', output.getvalue())

    def test_doctor_detects_enabled_profile_membership_drift(self):
        home = self._write_hermes_config('missing-agent')
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
            self.assertEqual(cm.exception.code, 1)
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn('missing_membership:missing-agent->demo', output.getvalue())

    def test_doctor_detects_duplicate_project_names_even_without_active_binding(self):
        conn = store.open_store(self.db)
        conn.execute('DROP INDEX IF EXISTS idx_project_name_unique')
        conn.execute("INSERT INTO project (id,name) VALUES ('project-a','duplicate-name')")
        conn.execute("INSERT INTO project (id,name) VALUES ('project-b','duplicate-name')")
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('project name collisions:', output.getvalue())
        self.assertIn('duplicate-name', output.getvalue())

    def test_doctor_detects_invalid_tombstone_scope_or_fingerprint(self):
        conn = store.open_store(self.db)
        conn.execute(
            "INSERT INTO tombstone (id,claim_fingerprint,scope,reason) "
            "VALUES ('t-bad','NOT-A-FINGERPRINT','private:missing-project:missing-agent','bad')"
        )
        conn.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('tombstone violations:', output.getvalue())
        self.assertIn('invalid_fingerprint', output.getvalue())
        self.assertIn('invalid_private_scope', output.getvalue())

    def test_doctor_detects_agent_name_collision(self):
        home = self._write_hermes_config('checker')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-checker','checker','checker')")
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-other','checker','other')")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-checker','member',datetime('now'))")
        conn.execute("INSERT INTO project_membership VALUES ('proj-demo','agent-other','member',datetime('now'))")
        conn.close()
        old = os.environ.get('HERMES_HOME')
        os.environ['HERMES_HOME'] = home
        output = io.StringIO()
        try:
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(output):
                cli.main(['--db', self.db, 'doctor'])
        finally:
            if old is None:
                os.environ.pop('HERMES_HOME', None)
            else:
                os.environ['HERMES_HOME'] = old
        self.assertIn("('demo', 'checker', 2)", output.getvalue())

    def test_doctor_fails_if_journal_mode_is_not_wal(self):
        raw = sqlite3.connect(self.db)
        try:
            mode = raw.execute('PRAGMA journal_mode=DELETE').fetchone()[0]
            self.assertEqual(mode.lower(), 'delete')
        finally:
            raw.close()
        output = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(output):
            cli.main(['--db', self.db, 'doctor'])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn('journal mode: delete', output.getvalue())

    def test_gc_rejects_negative_retention_without_mutating_store(self):
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO agent (id,name,profile_key) VALUES ('agent-a','a','a')")
        conn.execute(
            "INSERT INTO project_membership (project_id,agent_id,role) "
            "VALUES ('proj-demo','agent-a','member')"
        )
        mem_id, _ = core.create_memory(
            conn, 'proj-demo', 'agent-a', 'must survive negative gc', scope='project'
        )
        conn.close()
        with self.assertRaises(SystemExit):
            cli.main(['--db', self.db, 'gc', '--candidate-days', '-1', '--apply'])
        conn = store.open_store(self.db)
        try:
            lifecycle = conn.execute(
                'SELECT lifecycle FROM memory WHERE id=?', (mem_id,)
            ).fetchone()[0]
            self.assertEqual(lifecycle, 'candidate')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
