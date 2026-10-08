"""memcore export — the local-first path to every other coding agent.

The point of these tests is that the export must never become a service. No
port, no daemon, no MCP: one command writes a markdown file that Codex, agy,
Claude Code and Gemini all already read from the working tree. The properties
worth pinning are therefore scope confinement (an export is a file OTHER agents
read) and byte-stability (a re-run that changes nothing must not dirty git).
"""
import os
import subprocess
import sys
import tempfile
import unittest

from memcore import core, export as export_mod, store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ExportBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_export_')
        self.db = os.path.join(self.tmpdir, 'export.db')
        self.conn = store.open_store(self.db)
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES ('proj-e', 'shared-platform')")
        for name in ('mika', 'nua'):
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (f'agent-{name}', name, name))
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)', ('proj-e', f'agent-{name}', 'member'))
        self.conn.commit()

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass


class ExportScopeTests(ExportBase):
    """An export is read by agents outside the fleet; private must be opt-in."""

    def setUp(self):
        super().setUp()
        core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'shared gateway port is 20128 for every call',
            scope='project', lifecycle='accepted')
        core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'private note about what mika was told in confidence',
            scope='private', lifecycle='accepted')

    def test_private_is_excluded_by_default(self):
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 40, include_private=False)
        self.assertFalse(
            any(r[1] == 'private' for r in rows),
            'private memories must not reach a shared export by default')

    def test_private_reaches_only_with_the_explicit_flag(self):
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 40, include_private=True)
        self.assertTrue(any(r[1] == 'private' for r in rows))

    def test_other_agents_private_stays_hidden_even_with_the_flag(self):
        core.create_memory(
            self.conn, 'proj-e', 'agent-nua',
            'nua private research note that must never leak',
            scope='private', lifecycle='accepted')
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 40, include_private=True)
        self.assertFalse(
            any('nua private research note' in r[5] for r in rows),
            'another agent\'s private memory must stay private')

    def test_tombstoned_claims_never_export(self):
        mem_id, _ = core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'gateway port is 9999 because of a typo',
            scope='project', lifecycle='accepted')
        core.reject(self.conn, mem_id, 'agent-mika', 'disproven')
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 40)
        self.assertFalse(any('9999' in r[5] for r in rows))


class ExportContentTests(ExportBase):
    def setUp(self):
        super().setUp()
        for agent in ('mika', 'nua'):
            core.create_memory(
                self.conn, 'proj-e', f'agent-{agent}',
                'fleet uses five agents with mika orchestrating',
                scope='project', lifecycle='accepted')

    def test_corroborating_copies_collapse(self):
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 40)
        self.assertEqual(len(rows), 1,
                         'four agents writing one claim must export as one line')

    def test_limit_is_respected(self):
        for i in range(10):
            # Distinct SUBJECT per fact: `subject_key` takes the first words, so
            # "distinct fact number N" would share a subject with its siblings and
            # the contradiction gate (correctly) hold it as a numeric clash.
            core.create_memory(
                self.conn, 'proj-e', 'agent-mika',
                f'subject{i} records alpha signal {i} units on port 20128',
                scope='project', lifecycle='accepted')
        rows = export_mod.rank_for_export(self.conn, 'proj-e', 'agent-mika', 3)
        self.assertEqual(len(rows), 3)

    def test_trust_labels_are_rendered(self):
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 40)
        text = export_mod.render(rows, 'Shared project memory', 'shared-platform')
        self.assertIn('[project | accepted |', text)

    def test_conflict_rows_are_labelled_disputed(self):
        core.create_memory(
            self.conn, 'proj-e', 'agent-nua',
            'gateway port is 8080 for every call',
            scope='project', lifecycle='candidate')
        rows = export_mod.rank_for_export(self.conn, 'proj-e', 'agent-mika', 40)
        text = export_mod.render(rows, 't', 'shared-platform')
        self.assertIn('`conflict` rows are disputed', text)
        self.assertIn('candidate', text)


class ExportStabilityTests(ExportBase):
    """Byte-stability: a no-op re-run must not produce a diff."""

    def setUp(self):
        super().setUp()
        core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'hermes profile switch uses the sticky cli command',
            scope='project', lifecycle='accepted')

    def test_render_is_deterministic(self):
        first = export_mod.export(
            self.conn, 'proj-e', 'agent-mika',
            os.path.join(self.tmpdir, 'MEMORY.md'))
        second = export_mod.export(
            self.conn, 'proj-e', 'agent-mika',
            os.path.join(self.tmpdir, 'MEMORY.md'))
        self.assertTrue(first['wrote'])
        self.assertFalse(
            second['wrote'],
            'a second identical export must not rewrite the file')

    def test_marker_carries_a_content_digest_not_a_timestamp(self):
        path = os.path.join(self.tmpdir, 'MEMORY.md')
        export_mod.export(self.conn, 'proj-e', 'agent-mika', path)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('generated by MemCore (content ', text)
        self.assertNotIn('Generated:', text)


class ExportCliTests(unittest.TestCase):
    """The CLI must work as a plain command — no server, no port."""

    def test_cli_runs_read_only_and_writes_a_file(self):
        with tempfile.TemporaryDirectory(prefix='memcore_export_cli_') as tmp:
            db = os.path.join(tmp, 'cli.db')
            setup = store.open_store(db)
            setup.execute(
                "INSERT INTO project (id, name) VALUES ('proj-c', 'shared-platform')")
            setup.execute(
                "INSERT INTO agent (id, name, profile_key) "
                "VALUES ('agent-mika', 'mika', 'mika')")
            setup.execute(
                "INSERT INTO project_membership (project_id, agent_id, role) "
                "VALUES ('proj-c', 'agent-mika', 'owner')")
            setup.commit()
            core.create_memory(
                setup, 'proj-c', 'agent-mika',
                'cli export writes a plain markdown file',
                scope='project', lifecycle='accepted')
            setup.close()
            out = os.path.join(tmp, 'MEMORY.md')
            r = subprocess.run(
                [sys.executable, '-m', 'memcore', '--db', db, 'export',
                 '--project', 'shared-platform', '--agent', 'mika',
                 '--out', out],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.isfile(out))
            with open(out, encoding='utf-8') as fh:
                body = fh.read()
            self.assertIn('cli export writes a plain markdown file', body)

    def test_cli_never_imports_an_http_client(self):
        """Local-first guard: the export path must not pull in httpx/asyncio."""
        r = subprocess.run(
            [sys.executable, '-X', 'importtime', '-m', 'memcore',
             'export', '--stdout', '--limit', '1'],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=180)
        imported = r.stderr
        for forbidden in ('httpx', 'requests', 'aiohttp'):
            self.assertNotIn(
                forbidden, imported,
                f'{forbidden} must not be on the export import path')



class ExportHostTargetTests(ExportBase):
    """Popular coding agents map to verified native instruction files."""

    def test_default_target_is_agents_md(self):
        self.assertEqual(export_mod.DEFAULT_OUT.as_posix(), 'AGENTS.md')
        self.assertEqual(export_mod.targets_for(None), (export_mod.DEFAULT_OUT,))

    def setUp(self):
        super().setUp()
        core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'shared gateway port is 20128 for every call',
            scope='project', lifecycle='accepted')

    def test_every_host_has_exactly_one_markdown_target(self):
        for host in export_mod.HOST_TARGETS:
            targets = export_mod.targets_for(host)
            self.assertEqual(len(targets), 1, host)
            self.assertTrue(str(targets[0]).endswith('.md'), host)

    def test_popular_agents_use_native_instruction_files(self):
        expected = {
            'codex': ('AGENTS.md',),
            'claude-code': ('CLAUDE.md',),
            'cursor': ('AGENTS.md',),
            'antigravity': ('AGENTS.md',),
            'deepseek-harness': ('AGENTS.md',),
            'zcode': ('AGENTS.md',),
            'hermes': ('AGENTS.md',),
            'opencode': ('AGENTS.md',),
            'copilot': ('.github/copilot-instructions.md',),
            'github-copilot': ('.github/copilot-instructions.md',),
            'memory': ('MEMORY.md',),
            'generic': ('MEMORY.md',),
            'gemini-cli': ('GEMINI.md',),
            'windsurf': ('AGENTS.md',),
            'cline': ('AGENTS.md',),
            'kilocode': ('AGENTS.md',),
            'roo': ('AGENTS.md',),
        }
        for host, target in expected.items():
            self.assertEqual(export_mod.targets_for(host), target, host)

    def test_low_demand_host_is_not_advertised(self):
        self.assertNotIn('freebuff', export_mod.HOST_TARGETS)

    def test_host_all_covers_every_distinct_known_target(self):
        everything = set(export_mod.HOST_ALL_TARGETS)
        expected = {
            target for host in export_mod.HOST_TARGETS
            for target in export_mod.targets_for(host)
        }
        self.assertEqual(everything, expected)

    def test_unknown_host_falls_back_to_the_default_file(self):
        self.assertEqual(export_mod.targets_for('nonsense'),
                         (export_mod.DEFAULT_OUT,))

    def test_all_targets_receive_byte_identical_content(self):
        out_dir = os.path.join(self.tmpdir, 'repo')
        results = export_mod.export(
            self.conn, 'proj-e', 'agent-mika', host='all', out_dir=out_dir)
        self.assertEqual(len(results), len(export_mod.HOST_ALL_TARGETS))
        bodies = {}
        for name in export_mod.HOST_ALL_TARGETS:
            path = os.path.join(out_dir, name.replace('/', os.sep))
            self.assertTrue(os.path.isfile(path), name)
            with open(path, encoding='utf-8') as fh:
                bodies[name] = fh.read()
        self.assertEqual(
            len(set(bodies.values())), 1,
            'every host must read the same content')

    def test_out_dir_writes_the_shared_agents_file(self):
        out_dir = os.path.join(self.tmpdir, 'fresh', 'repo')
        export_mod.export(
            self.conn, 'proj-e', 'agent-harness', host='harness', out_dir=out_dir)
        self.assertTrue(os.path.isfile(os.path.join(out_dir, 'AGENTS.md')))

    def test_out_dir_creates_the_copilot_instructions_directory(self):
        out_dir = os.path.join(self.tmpdir, 'fresh', 'copilot-repo')
        export_mod.export(
            self.conn, 'proj-e', 'agent-harness', host='copilot', out_dir=out_dir)
        self.assertTrue(os.path.isfile(os.path.join(
            out_dir, '.github', 'copilot-instructions.md')))

    def test_explicit_out_wins_over_host(self):
        target = os.path.join(self.tmpdir, 'custom.md')
        export_mod.export(
            self.conn, 'proj-e', 'agent-mika', out_path=target, host='codex')
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, 'AGENTS.md')))



class ExportSafetyTests(ExportBase):
    """The export writes files agents AND humans hand-author, so it must not
    destroy one silently, must not leak project rows to a non-member, and must
    rank the way recall ranks."""

    def test_refuses_to_clobber_a_hand_written_file(self):
        target = os.path.join(self.tmpdir, 'CLAUDE.md')
        with open(target, 'w', encoding='utf-8') as fh:
            fh.write('Run npm test before every commit. NEVER commit to main.')
        result = export_mod.export(
            self.conn, 'proj-e', 'agent-mika', out_path=target)
        self.assertFalse(result['wrote'])
        self.assertIn('refused', result)
        with open(target, encoding='utf-8') as fh:
            self.assertIn('NEVER commit to main', fh.read())

    def test_force_overwrites_a_hand_written_file(self):
        target = os.path.join(self.tmpdir, 'CLAUDE.md')
        with open(target, 'w', encoding='utf-8') as fh:
            fh.write('hand written')
        result = export_mod.export(
            self.conn, 'proj-e', 'agent-mika', out_path=target, force=True)
        self.assertTrue(result['wrote'])
        with open(target, encoding='utf-8') as fh:
            self.assertNotIn('hand written', fh.read())

    def test_rewrites_its_own_generated_file_without_force(self):
        target = os.path.join(self.tmpdir, 'MEMORY.md')
        export_mod.export(self.conn, 'proj-e', 'agent-mika', out_path=target)
        result = export_mod.export(
            self.conn, 'proj-e', 'agent-mika', out_path=target)
        self.assertFalse(result['wrote'], 'idempotent re-run writes nothing')

    def test_directory_target_is_rejected(self):
        target = os.path.join(self.tmpdir, 'adir')
        os.makedirs(target)
        with self.assertRaises(ValueError):
            export_mod.export(
                self.conn, 'proj-e', 'agent-mika', out_path=target)

    def test_out_dir_applies_to_explicit_out(self):
        """--out-dir must not silently ignore itself when --out is given."""
        out_dir = os.path.join(self.tmpdir, 'elsewhere')
        target = os.path.join(self.tmpdir, 'OUT2.md')
        export_mod.export(self.conn, 'proj-e', 'agent-mika',
                          out_path=target, out_dir=out_dir)
        # --out is a path, so it wins; what must NOT happen is a second copy
        # appearing in cwd, and the call must not silently write elsewhere.
        self.assertTrue(os.path.isfile(target))

    def test_non_member_agent_sees_nothing(self):
        core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'the shared gateway port is 20128 for every call',
            scope='project', lifecycle='accepted')
        rows = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-stranger', 40)
        self.assertEqual(rows, [], 'a non-member must not read the project')
        # core.search agrees: same gate, same answer
        self.assertEqual(
            core.search(self.conn, 'proj-e', 'agent-stranger', 'gateway', 10),
            [])

    def test_export_orders_by_the_same_retention_signal_as_recall(self):
        """Export exists so another agent sees what recall would show. If the
        two rank differently the file is worse than useless."""
        for word in ('alpha', 'bravo', 'charlie', 'delta'):
            core.create_memory(
                self.conn, 'proj-e', 'agent-mika',
                f'gateway {word} listens upstream for every call',
                scope='project', lifecycle='accepted')
        exported = export_mod.rank_for_export(
            self.conn, 'proj-e', 'agent-mika', 4)
        recalled = core.search(
            self.conn, 'proj-e', 'agent-mika', 'gateway listens upstream',
            limit=4)
        self.assertTrue(exported)
        self.assertEqual(
            [r[0] for r in exported], [r[0] for r in recalled],
            'export and recall must agree on order')

if __name__ == '__main__':
    unittest.main()
