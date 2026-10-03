"""Regression tests for the deployed plugin's MemCore import resolution.

The real-world failure (2026-09-26, Hermes on Windows): Hermes puts the process
cwd on ``sys.path``, so when it is launched from a directory containing a plain
``memcore/`` folder, ``find_spec('memcore')`` succeeds on a PEP 420 *namespace*
package. The plugin guard read that as "engine already importable", never added
the real checkout to ``sys.path``, and every ``from memcore import core`` raised
"cannot import name 'core' from 'memcore' (unknown location)" — the memory
provider silently failed to load for the whole fleet.

``memcore/memcore/`` has no ``__init__.py``, so the genuine engine is *itself* a
namespace package and an ``origin is not None`` check cannot tell the two apart.
The only sound predicate is whether the ``memcore.core`` SUBMODULE resolves.

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
sys.path = [str(SHADOW_CWD)] + [p for p in sys.path if p and "site-packages" not in p]
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
        """Document the trap: ``find_spec('memcore')`` cannot see the difference,
        while ``find_spec('memcore.core')`` can. Measured BEFORE the guard runs."""
        out = self._run_guard('''
        spec = importlib.util.find_spec('memcore')
        print('FIND_SPEC', spec is not None)
        print('CORE_SPEC', importlib.util.find_spec('memcore.core') is not None)
        ''', block=self.definitions_only)
        self.assertIn('FIND_SPEC True', out)
        self.assertIn('CORE_SPEC False', out)

    def test_guard_inserts_checkout_when_namespace_shadows_the_engine(self):
        out = self._run_guard('''
        _ensure_memcore_importable()
        from memcore import core
        print('CORE', pathlib.Path(core.__file__).name)
        print('REPO_ON_PATH', str(REPO) in sys.path)
        ''')
        self.assertIn('CORE core.py', out)
        self.assertIn('REPO_ON_PATH True', out)

    def test_guard_is_idempotent_and_full_engine_imports(self):
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


if __name__ == '__main__':
    unittest.main()