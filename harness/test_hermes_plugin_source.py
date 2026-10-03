"""Regression tests for the Git-tracked Hermes plugin source and deployer."""
import importlib.util
import pathlib
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / 'integrations' / 'hermes' / 'memcore'
DEPLOY_PATH = REPO_ROOT / 'scripts' / 'deploy_hermes_plugin.py'
_spec = importlib.util.spec_from_file_location('memcore_deploy_hermes_plugin', DEPLOY_PATH)
deploy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deploy)


class HermesPluginSourceTests(unittest.TestCase):
    def test_runtime_source_is_fully_tracked_by_explicit_allowlist(self):
        self.assertEqual(set(deploy.RUNTIME_FILES), {
            '__init__.py', 'plugin.yaml', 'plugin.py', 'native_provider.py',
            'semantic_analyzer.py', 'README.md',
        })
        for relative in deploy.RUNTIME_FILES:
            self.assertTrue((PLUGIN_ROOT / pathlib.PurePosixPath(relative)).is_file(), relative)
            self.assertNotIn('__pycache__', relative)
            self.assertNotIn('/tests/', '/' + relative)

    def test_no_dashboard_or_desktop_remnants(self):
        self.assertFalse((PLUGIN_ROOT / 'dashboard').exists())
        self.assertFalse((PLUGIN_ROOT / 'desktop').exists())

    def test_plugin_version_is_0_8_0(self):
        plugin_yaml = (PLUGIN_ROOT / 'plugin.yaml').read_text(encoding='utf-8')
        version_line = next(
            line for line in plugin_yaml.splitlines() if line.startswith('version:')
        )
        version = version_line.split(':', 1)[1].strip()
        self.assertEqual(version, '0.8.0')

    def test_source_has_no_machine_specific_absolute_checkout(self):
        for relative in ('plugin.py', 'native_provider.py'):
            text = (PLUGIN_ROOT / pathlib.PurePosixPath(relative)).read_text(encoding='utf-8')
            self.assertNotIn('C:\\Users\\BlankScreen', text)
        provider = (PLUGIN_ROOT / 'native_provider.py').read_text(encoding='utf-8')
        self.assertNotIn('_MEMCORE_SRC', provider)
        self.assertIn("os.environ.get('MEMCORE_SRC')", (
            PLUGIN_ROOT / 'plugin.py'
        ).read_text(encoding='utf-8'))

    def test_deployer_dry_run_then_deploy_then_check(self):
        with tempfile.TemporaryDirectory(prefix='memcore_plugin_deploy_') as root:
            target = pathlib.Path(root) / 'plugins' / 'memcore'
            self.assertEqual(deploy.main(['--target', str(target), '--dry-run']), 0)
            self.assertFalse(target.exists())
            self.assertEqual(deploy.main(['--target', str(target)]), 0)
            self.assertEqual(deploy.main(['--target', str(target), '--check']), 0)
            for relative in deploy.RUNTIME_FILES:
                source = PLUGIN_ROOT / pathlib.PurePosixPath(relative)
                installed = target / pathlib.PurePosixPath(relative)
                self.assertEqual(deploy.sha256(source), deploy.sha256(installed))

    def test_check_detects_drift_without_repairing_it(self):
        with tempfile.TemporaryDirectory(prefix='memcore_plugin_drift_') as root:
            target = pathlib.Path(root) / 'memcore'
            self.assertEqual(deploy.main(['--target', str(target)]), 0)
            drifted = target / 'plugin.yaml'
            drifted.write_text('drifted\n', encoding='utf-8')
            before = drifted.read_bytes()
            self.assertEqual(deploy.main(['--target', str(target), '--check']), 1)
            self.assertEqual(drifted.read_bytes(), before)

    def test_atomic_copy_falls_back_when_windows_replace_is_denied(self):
        with tempfile.TemporaryDirectory(prefix='memcore_plugin_replace_') as root:
            root = pathlib.Path(root)
            source = root / 'source.txt'
            target = root / 'target.txt'
            source.write_text('new content', encoding='utf-8')
            target.write_text('old content', encoding='utf-8')
            original_replace = deploy.os.replace
            deploy.os.replace = lambda *_args: (_ for _ in ()).throw(
                PermissionError('simulated loader handle')
            )
            try:
                deploy._atomic_copy(source, target)
            finally:
                deploy.os.replace = original_replace
            self.assertEqual(target.read_text(encoding='utf-8'), 'new content')


if __name__ == '__main__':
    unittest.main()
