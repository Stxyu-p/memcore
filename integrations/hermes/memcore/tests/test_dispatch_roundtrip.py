"""Dispatch round-trip regression: a memory tool call must NEVER touch the real store.

Incident (2026-10-03): an integration probe intended to prove ingestion end-to-end
injected a throwaway ``store_path`` into the config it passed to the provider —
but ``MemCoreMemoryProvider.initialize()`` calls ``_load_config()``
(``load_config_readonly()``, native_provider.py:75), which ignores the injected
dict and reads the real ``config.yaml``. The probe's test memory landed in
production ``~/.memcore/memory.db`` as an ``accepted`` row. Only one row, and it
was soft-deleted — but nothing in the suite would have caught it.

The invariant these tests lock down:
  1. Overriding ``_load_config`` genuinely redirects every store the provider
     touches (the mock is not decorative).
  2. A ``memory_remember`` -> ``memory_search`` round trip writes AND reads back
     a governed row through the same routing Hermes uses in production.
  3. The row lands in the injected store, never in the user's real store.

They also pin the ADR-0019 / ADR-0018 split: an explicit tool write is accepted
immediately (deliberate durability) but stays ``unverified`` until independent
agents corroborate — one source is not two.
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
for _path in (REPO_ROOT, PLUGIN_ROOT):
    _value = str(_path)
    if _value not in sys.path:
        sys.path.insert(0, _value)

from memcore import store
from native_provider import MemCoreMemoryProvider, agent_plugin


REAL_STORE = pathlib.Path(os.path.expanduser('~/.memcore/memory.db'))
PROBE_CONTENT = 'MIKA briefs bots in English and speaks Thai only with P Choke.'


def config_for(path, agent='alice'):
    return {
        'plugins': {'entries': {'memcore': {'settings': {
            'store_path': path,
            'default_project': 'demo',
            'agent_name': agent,
            'inject': {'budget_chars': 1200, 'max_items': 8},
        }}}}
    }


def _real_store_fingerprint():
    """(exists, memory count) of the user's real store, or None when absent."""
    if not REAL_STORE.exists():
        return None
    conn = store.open_store_readonly(str(REAL_STORE))
    try:
        return conn.execute('SELECT count(*) FROM memory').fetchone()[0]
    finally:
        conn.close()


class DispatchRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_dispatch_')
        self.db = os.path.join(self.tmp.name, 'memory.db')
        conn = store.open_store(self.db)
        conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo','demo')")
        conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-alice','alice','alice')")
        conn.execute(
            "INSERT INTO project_membership (project_id, agent_id, role)"
            " VALUES ('proj-demo','agent-alice','owner')"
        )
        conn.close()
        self.real_before = _real_store_fingerprint()

    def tearDown(self):
        agent_plugin.reset_conn()
        self.tmp.cleanup()

    def provider(self):
        p = MemCoreMemoryProvider()
        # The one seam that redirects the store. If this ever stops being the
        # path initialize() reads, the isolation test below fails loudly.
        p._load_config = lambda: config_for(self.db)
        p.initialize('session-dispatch', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        return p

    def test_config_override_redirects_the_store(self):
        """The mock is load-bearing, not decorative."""
        p = self.provider()
        self.assertEqual(p._store_path, self.db)
        self.assertNotEqual(pathlib.Path(p._store_path).resolve(), REAL_STORE.resolve())

    def test_remember_then_search_round_trip(self):
        p = self.provider()
        names = [s['name'] for s in p.get_tool_schemas()]
        self.assertIn('memory_remember', names)
        self.assertIn('memory_search', names)

        written = json.loads(p.handle_tool_call('memory_remember', {
            'content': PROBE_CONTENT, 'type': 'decision',
        }))
        self.assertTrue(written.get('success'), written)
        memory_id = written.get('memory_id') or written.get('id')
        self.assertTrue(memory_id, written)

        found = json.loads(p.handle_tool_call('memory_search', {'query': 'English Thai'}))
        rows = found.get('results') or found.get('memories') or []
        self.assertTrue(
            any(PROBE_CONTENT[:30] in (r.get('content') or '') for r in rows),
            'written memory was not retrievable: %r' % (rows[:2],),
        )

    def test_explicit_write_is_accepted_but_unverified(self):
        """ADR-0019 accepts deliberate writes; ADR-0018 keeps verification unclaimed."""
        p = self.provider()
        written = json.loads(p.handle_tool_call('memory_remember', {
            'content': PROBE_CONTENT, 'type': 'decision',
        }))
        memory_id = written.get('memory_id') or written.get('id')

        conn = store.open_store(self.db)
        try:
            row = conn.execute(
                'SELECT lifecycle, verification, scope FROM memory WHERE id=?', (memory_id,)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, ('accepted', 'unverified', 'project'))

    def test_round_trip_never_writes_the_real_store(self):
        """The regression that would have caught the 2026-10-03 incident."""
        if self.real_before is None:
            self.skipTest('no real user store at %s' % REAL_STORE)
        p = self.provider()
        written = json.loads(p.handle_tool_call('memory_remember', {
            'content': PROBE_CONTENT, 'type': 'decision',
        }))
        self.assertTrue(written.get('success'), written)

        real_after = _real_store_fingerprint()
        self.assertEqual(
            real_after, self.real_before,
            'memory tool call mutated the real user store %s' % REAL_STORE,
        )
        conn = store.open_store_readonly(str(REAL_STORE))
        try:
            leaked = conn.execute(
                'SELECT count(*) FROM memory_version WHERE content LIKE ?', (PROBE_CONTENT[:30],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(leaked, 0, 'probe content leaked into the real store')


class StoreLeakDetectorTests(unittest.TestCase):
    """Negative control: prove the isolation assertions actually fail on a leak.

    Running the leak against the user's real store to 'prove' the guard works is
    the same mistake the guard exists to prevent. Here the leak is aimed at a
    stand-in store in a temp dir, so the failure is real but the blast radius is
    zero.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_leak_')
        self.injected_db = os.path.join(self.tmp.name, 'injected.db')
        self.real_db = os.path.join(self.tmp.name, 'real.db')
        for path in (self.injected_db, self.real_db):
            conn = store.open_store(path)
            conn.execute("INSERT INTO project (id, name) VALUES ('proj-demo','demo')")
            conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('agent-alice','alice','alice')")
            conn.execute(
                "INSERT INTO project_membership (project_id, agent_id, role)"
                " VALUES ('proj-demo','agent-alice','owner')"
            )
            conn.close()
        agent_plugin.reset_conn()

    def tearDown(self):
        agent_plugin.reset_conn()
        self.tmp.cleanup()

    def _count(self, path):
        conn = store.open_store_readonly(path)
        try:
            return conn.execute('SELECT count(*) FROM memory').fetchone()[0]
        finally:
            conn.close()

    def test_leak_is_detected_by_the_same_fingerprint_check(self):
        """Without the config override the write lands in the real config's store."""
        p = MemCoreMemoryProvider()
        p._load_config = lambda: config_for(self.real_db)   # simulates the incident
        p.initialize('session-leak', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        before = self._count(self.real_db)
        self.assertEqual(p._store_path, self.real_db)

        written = json.loads(p.handle_tool_call('memory_remember', {
            'content': PROBE_CONTENT, 'type': 'decision',
        }))
        self.assertTrue(written.get('success'), written)

        # The leak IS observable — so test_round_trip_never_writes_the_real_store
        # is a real guard, not a tautology.
        self.assertNotEqual(self._count(self.real_db), before)
        self.assertEqual(self._count(self.injected_db), 0)

    def test_configured_store_keeps_the_write_out_of_the_real_store(self):
        """With the override in place the same call stays in the injected store."""
        p = MemCoreMemoryProvider()
        p._load_config = lambda: config_for(self.injected_db)
        p.initialize('session-clean', hermes_home=self.tmp.name,
                     platform='cli', agent_identity='alice')
        before = self._count(self.real_db)

        written = json.loads(p.handle_tool_call('memory_remember', {
            'content': PROBE_CONTENT, 'type': 'decision',
        }))
        self.assertTrue(written.get('success'), written)

        self.assertEqual(self._count(self.real_db), before)
        self.assertEqual(self._count(self.injected_db), 1)


if __name__ == '__main__':
    unittest.main()