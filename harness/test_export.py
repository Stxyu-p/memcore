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
        self.db_path = os.path.join(self.tmpdir, 'export.db')
        self.conn = store.open_store(self.db_path)
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
                os.unlink(self.db_path + suffix)
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
    """--host must write the file each agent family actually reads.

    Evidence recorded in export.HOST_TARGETS: codex and agy binaries were
    scanned for convention filenames, and freebuff's own --help names `.agents`
    files. Content must be byte-identical across targets — divergent per-host
    files are how two agents end up with two different truths.
    """

    def setUp(self):
        super().setUp()
        core.create_memory(
            self.conn, 'proj-e', 'agent-mika',
            'shared gateway port is 20128 for every call',
            scope='project', lifecycle='accepted')

    def test_every_host_has_exactly_one_primary_target(self):
        for host in ('codex', 'agy', 'freebuff', 'claude'):
            targets = export_mod.targets_for(host)
            self.assertEqual(len(targets), 1, host)
            self.assertTrue(targets[0].endswith('.md'), host)

    def test_targets_are_the_verified_conventions(self):
        self.assertEqual(export_mod.targets_for('codex')[0], 'MEMORY.md')
        self.assertEqual(export_mod.targets_for('agy')[0], 'GEMINI.md')
        self.assertEqual(export_mod.targets_for('claude')[0], 'CLAUDE.md')
        self.assertTrue(
            export_mod.targets_for('freebuff')[0].startswith('.agents/'),
            'freebuff loads `.agents` files per its own --help')

    def test_host_all_covers_every_known_host(self):
        everything = set(export_mod.HOST_ALL_TARGETS)
        for host in ('codex', 'agy', 'claude', 'freebuff'):
            self.assertTrue(set(export_mod.targets_for(host)) <= everything, host)

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

    def test_out_dir_creates_nested_paths(self):
        out_dir = os.path.join(self.tmpdir, 'fresh', 'repo')
        export_mod.export(
            self.conn, 'proj-e', 'agent-mika', host='freebuff', out_dir=out_dir)
        self.assertTrue(os.path.isfile(
            os.path.join(out_dir, '.agents', 'memory.md')))

    def test_explicit_out_wins_over_host(self):
        target = os.path.join(self.tmpdir, 'custom.md')
        export_mod.export(
            self.conn, 'proj-e', 'agent-mika', out_path=target, host='codex')
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(
            os.path.join(self.tmpdir, 'MEMORY.md')))


if __name__ == '__main__':
    unittest.main()
