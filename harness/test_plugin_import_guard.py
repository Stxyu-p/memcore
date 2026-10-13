"""Regression tests for the deployed plugin's MemCore import resolution.

The real-world failure (2026-09-26, Hermes on Windows): Hermes puts the process
cwd on ``sys.path``, so when it is launched from a directory containing a plain
``memcore/`` folder, ``find_spec('memcore')`` succeeds on a PEP 420 *namespace*
package. The plugin guard read that as "engine already importable", never added
the real checkout to ``sys.path``, and every ``from memcore import core`` raised
"cannot import name 'core' from 'memcore' (unknown location)" — the memory
provider silently failed to load for the whole fleet.

The fix (2026-10-11): ``memcore/memcore/`` MUST NOT contain ``__init__.py``.
This makes the genuine engine a *pure* PEP 420 namespace package (no module
object at the top level), so ``find_spec('memcore')`` will always return a spec
without ``origin``. The guard correctly rejects it and adds the real checkout.

Each test runs the real guard body in a subprocess with a synthetic ``sys.path``,
so the assertions cannot be fooled by this process's own imports.
"""
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_SOURCE = REPO_ROOT / 'integrations' / 'hermes' / 'memcore' / 'plugin.py'

PROBE_PREAMBLE = '''
import importlib.util, os, pathlib, sys
SHADOW_CWD = pathlib.Path(sys.argv[1])
REPO = pathlib.Path(sys.argv[2])
# plugin.py's own module-level constant, pointed at the source tree under test.
_PLUGIN_DIR = REPO / "integrations" / "hermes" / "memcore"
# Mirror Hermes: cwd on sys.path, engine checkout NOT yet present.
# Remove '', site-packages, .venv, editable memcore installs, and any path containing "memcore"
# so only the shadow directory is on sys.path.
def _clean_path(p: str) -> bool:
    if not p:
        return False
    if "site-packages" in p:
        return False
    if ".venv" in p:
        return False
    if "__editable__" in p:
        return False
    if "memcore" in p.lower():
        return False
    return True
sys.path = [str(SHADOW_CWD)] + [p for p in sys.path if _clean_path(p)]
# Also remove editable finders from meta_path that could resolve memcore
sys.meta_path = [f for f in sys.meta_path if "memcore" not in str(f).lower() and "editable" not in str(f).lower()]
sys.path_hooks = [h for h in sys.path_hooks if "memcore" not in str(h).lower() and "editable" not in str(h).lower()]
sys.path_importer_cache.clear()
os.chdir(SHADOW_CWD)
for _m in [m for m in list(sys.modules) if m == "memcore" or m.startswith("memcore.")]:
    del sys.modules[_m]
'''


def _extract_block(include_call_site: bool = True) -> str:
    """plugin.py's import-resolution block verbatim: the NOTE, the engine probe,
    ``_ensure_memcore_importable``, and (unless suppressed) its module-level call."""
    source = PLUGIN_SOURCE.read_text(encoding='utf-8')
    start = source.index('# NOTE: never add _PLUGIN_DIR itself to sys.path')
    end = source.index('_ensure_memcore_importable()\n', start) + len('_ensure_memcore_importable()\n')
    block = textwrap.dedent(source[start:end])
    if include_call_site:
        return block
    return block[:block.rindex('_ensure_memcore_importable()\n')]


class PluginImportGuardTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='memcore_guard_')
        self.addCleanup(self._tmp.cleanup)
        self.shadow = pathlib.Path(self._tmp.name) / 'shadow_workspace'
        (self.shadow / 'memcore').mkdir(parents=True)
        # No __init__.py — this is the fix: the shadow is a bare namespace package
        (self.shadow / 'memcore' / 'unrelated.txt').write_text('not the engine\n', encoding='utf-8')
        self.block = _extract_block()
        self.definitions_only = _extract_block(include_call_site=False)

    def _run_guard(self, body: str, block: str | None = None) -> str:
        script = PROBE_PREAMBLE + (self.block if block is None else block) + textwrap.dedent(body)
        result = subprocess.run(
            [sys.executable, '-c', script, str(self.shadow), str(REPO_ROOT)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, f'stdout={result.stdout!r} stderr={result.stderr!r}')
        return result.stdout.strip()

    def test_shadow_dir_alone_looks_importable_to_find_spec(self):
        """Document the trap: ``find_spec('memcore')`` succeeds (PEP 420 namespace),
        while ``find_spec('memcore.core')`` correctly fails. Measured BEFORE the guard runs."""
        out = self._run_guard('''
        spec = importlib.util.find_spec('memcore')
        print('FIND_SPEC', spec is not None)
        print('CORE_SPEC', importlib.util.find_spec('memcore.core') is not None)
        ''', block=self.definitions_only)
        self.assertIn('FIND_SPEC True', out)
        self.assertIn('CORE_SPEC False', out)

    def test_guard_inserts_checkout_when_namespace_shadows_the_engine(self):
        """The guard must detect the bare namespace package and add the real checkout."""
        out = self._run_guard('''
        _ensure_memcore_importable()
        from memcore import core
        print('CORE', pathlib.Path(core.__file__).name)
        print('REPO_ON_PATH', str(REPO) in sys.path)
        ''')
        self.assertIn('CORE core.py', out)
        self.assertIn('REPO_ON_PATH True', out)

    def test_guard_is_idempotent_and_full_engine_imports(self):
        """Calling the guard twice must not duplicate the repo path on sys.path."""
        out = self._run_guard('''
        _ensure_memcore_importable()
        _ensure_memcore_importable()
        from memcore import core, ingest, semantic, store
        print('ALL_OK', pathlib.Path(core.__file__).name)
        print('DUPES', sys.path.count(str(REPO)))
        ''')
        self.assertIn('ALL_OK core.py', out)
        self.assertIn('DUPES 1', out)

    def test_guard_prefers_already_importable_real_engine(self):
        """A genuinely importable engine must not be displaced by a later checkout."""
        out = self._run_guard('''
        sys.path.insert(0, str(REPO))
        _ensure_memcore_importable()
        from memcore import core
        print('CORE', pathlib.Path(core.__file__).name)
        print('PARENT', pathlib.Path(core.__file__).parent.parent.name)
        ''')
        self.assertIn('CORE core.py', out)
        self.assertIn('PARENT memcore', out)

    def test_no_init_py_in_engine_root(self):
        """The engine root (memcore/) MUST NOT contain __init__.py to avoid shadowing."""
        engine_root = REPO_ROOT / 'memcore'
        init_py = engine_root / '__init__.py'
        self.assertFalse(init_py.exists(), 'memcore/__init__.py must not exist — it breaks the plugin import guard')


if __name__ == '__main__':
    unittest.main()