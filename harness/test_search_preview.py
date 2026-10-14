"""Gap-1 RED: tool_memory_search preview mode (L0 progressive cap).

Default must return a truncated preview, NOT full bodies.
Full bodies only on explicit include_content=True.
Isolated tmp stores; NEVER touches live ~/.memcore/memory.db.
"""
import json
import pathlib
import sys
import tempfile
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / 'integrations' / 'hermes' / 'memcore'
sys.path.insert(0, str(PLUGIN_ROOT))

import plugin  # noqa: E402


def make_config(store_path=None, project='demo', agent=None, auto_join=False):
    memcore = {'default_project': project}
    if store_path:
        memcore['store_path'] = store_path
    if agent:
        memcore['agent_name'] = agent
    if auto_join:
        memcore['auto_join'] = True
    memcore['inject'] = {'budget_chars': 1200, 'max_items': 8}
    return {'plugins': {'memcore': memcore}}


LONG_BODY = (
    'Deploy pipeline: container image pinned by digest, k8s rollout with '
    'readiness probe, postgres analytics with pg_stat_statements, gateway '
    'auth via rotating token, cache redis with TTL, queue worker async with '
    'backoff, microservice latency SLO p95 under 200ms, scaling HPA on CPU '
    'and queue depth. '
) * 3  # ~900 chars


class TestSearchPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='memcore_preview_')
        self.store = str(pathlib.Path(self.tmp.name) / 'memory.db')
        plugin.reset_conn()
        plugin.auto_join({'config': make_config(
            self.store, 'demo', agent='sora', auto_join=True),
            'profile_name': 'sora'})
        plugin.auto_join({'config': make_config(
            self.store, 'demo', agent='mika', auto_join=True),
            'profile_name': 'mika'})

    def tearDown(self):
        plugin.reset_conn()
        self.tmp.cleanup()

    def ctx(self, agent):
        return {'config': make_config(self.store, 'demo'),
                'profile_name': agent}

    def test_default_returns_preview_not_full(self):
        body = ('PREVIEWPROBE ' + LONG_BODY).strip()
        out = json.loads(plugin.tool_memory_remember(
            {'content': body}, self.ctx('sora')))
        self.assertTrue(out['success'], out)
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'PREVIEWPROBE deploy pipeline'}, self.ctx('mika')))
        self.assertTrue(seen['success'], seen)
        self.assertEqual(len(seen['results']), 1)
        row = seen['results'][0]
        self.assertLess(len(row['content']), len(body))
        self.assertLessEqual(len(row['content']), 181)
        self.assertTrue(row['content_truncated'])
        self.assertEqual(row['content_chars'], len(body))

    def test_include_content_returns_full(self):
        body = ('FULLPROBE ' + LONG_BODY).strip()
        out = json.loads(plugin.tool_memory_remember(
            {'content': body}, self.ctx('sora')))
        self.assertTrue(out['success'], out)
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'FULLPROBE deploy pipeline', 'include_content': True},
            self.ctx('mika')))
        self.assertTrue(seen['success'], seen)
        row = seen['results'][0]
        self.assertEqual(row['content'], body)
        self.assertFalse(row['content_truncated'])
        self.assertEqual(row['content_chars'], len(body))

    def test_short_content_untouched(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'SHORTPROBE go build tiny fact'}, self.ctx('sora')))
        self.assertTrue(out['success'], out)
        seen = json.loads(plugin.tool_memory_search(
            {'query': 'SHORTPROBE go build'}, self.ctx('mika')))
        self.assertTrue(seen['success'], seen)
        row = seen['results'][0]
        self.assertEqual(row['content'], 'SHORTPROBE go build tiny fact')
        self.assertFalse(row['content_truncated'])

    def test_hostile_preview_chars_clamped(self):
        out = json.loads(plugin.tool_memory_remember(
            {'content': 'HOSTILEPROBE ' + LONG_BODY}, self.ctx('sora')))
        self.assertTrue(out['success'], out)
        for hostile, ceiling in ((5, 41), (99999, 501), ('bad', 181)):
            seen = json.loads(plugin.tool_memory_search(
                {'query': 'HOSTILEPROBE deploy',
                 'preview_chars': hostile}, self.ctx('mika')))
            self.assertTrue(seen['success'], seen)
            self.assertLessEqual(
                len(seen['results'][0]['content']), ceiling, hostile)


if __name__ == '__main__':
    unittest.main()
