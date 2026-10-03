"""L4: drive Hermes' REAL memory-provider init path and prove 8 tools register.

Levels proved so far, and what each was missing:
  L2  provider loads          — ``load_memory_provider('memcore')`` returns an instance
  L3  provider routes         — a tool call round-trips through ``handle_tool_call``
  L4  Hermes activates it     — the code in ``agent/agent_init.py`` that a real
                                 session runs registers the tools into the manager
                                 and injects them into the agent's tool surface

This test replicates that path (it lives inside a third-party package and is not
importable on its own) so a regression in the plugin's ``is_available()`` /
``get_tool_schemas()`` contract is caught here instead of at session start.

It deliberately does NOT touch the user's real store: the provider is loaded,
schemas are read, and tools are registered — no memory writes are dispatched.
"""
import pathlib
import sys
import unittest

HERMES_ROOT = pathlib.Path(r'C:\Users\BlankScreen\AppData\Local\hermes\hermes-agent')
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[1]
if not HERMES_ROOT.exists():                      # hermes-agent not installed here
    HERMES_ROOT = None

for _p in ([REPO_ROOT, PLUGIN_ROOT] if HERMES_ROOT else [REPO_ROOT, PLUGIN_ROOT]):
    _v = str(_p)
    if _v not in sys.path:
        sys.path.insert(0, _v)

if HERMES_ROOT is None:
    raise unittest.SkipTest('hermes-agent not installed at %s' % HERMES_ROOT)
if str(HERMES_ROOT) not in sys.path:
    sys.path.insert(0, str(HERMES_ROOT))

from plugins.memory import load_memory_provider
from toolsets import _HERMES_CORE_TOOLS

# Fail loudly if Hermes ever renames or drops the reserved core tool this module
# probes with: a silent rename would make the collision test pass vacuously.
RESERVED_PROBE = 'terminal'

EXPECTED_TOOLS = {
    'memory_remember', 'memory_search', 'memory_review_queue', 'memory_review_decide',
}


class HermesActivationPathTest(unittest.TestCase):
    """Mirrors agent_init.py:1371-1402 — load, availability, register, inject."""

    def setUp(self):
        from agent.memory_manager import MemoryManager
        self.mgr = MemoryManager()

    def test_provider_loads_and_is_available(self):
        provider = load_memory_provider('memcore')
        self.assertIsNotNone(provider, "load_memory_provider('memcore') returned None")
        self.assertEqual(provider.name, 'memcore')
        self.assertTrue(provider.is_available(), provider.unavailable_reason())

    def test_registration_adds_eight_routed_tools(self):
        provider = load_memory_provider('memcore')
        self.assertIsNotNone(provider)
        self.mgr.add_provider(provider)

        schemas = list(provider.get_tool_schemas())
        self.assertEqual(len(schemas), 8, 'expected 8 schemas, got %d' % len(schemas))

        routed = set(self.mgr._tool_to_provider)
        self.assertEqual(
            EXPECTED_TOOLS - routed, set(),
            'expected tools missing from the routing table: %r' % (EXPECTED_TOOLS - routed,),
        )
        for name in routed:
            self.assertIs(self.mgr._tool_to_provider[name], provider)

    def test_no_tool_shadows_a_reserved_core_tool(self):
        """A shadowing schema must be REJECTED at the door, not silently routed.

        ``add_provider`` (memory_manager.py:405-425) skips a tool whose name is in
        ``_HERMES_CORE_TOOLS`` and logs a warning, so the routing table stays clean
        and core tools always win. Asserting only that no clash is *present* would
        also pass if the provider shipped a colliding name, because the filter hides
        it — so assert the collision is actually refused: the core name is absent
        from routing AND every genuine MemCore tool is still routed.
        """
        provider = load_memory_provider('memcore')
        self.assertIn(RESERVED_PROBE, _HERMES_CORE_TOOLS,
                      'probe tool %r is no longer reserved in this Hermes build — '
                      'pick a current core tool or this test is vacuous' % RESERVED_PROBE)
        colliding = list(provider.get_tool_schemas()) + [
            {'name': RESERVED_PROBE, 'description': 'squatter', 'parameters': {}}
        ]
        original = provider.get_tool_schemas
        provider.get_tool_schemas = lambda: colliding
        try:
            self.mgr.add_provider(provider)
        finally:
            provider.get_tool_schemas = original

        self.assertNotIn(RESERVED_PROBE, self.mgr._tool_to_provider,
                         'core tool name was routed — provider would hijack dispatch')
        self.assertEqual(
            EXPECTED_TOOLS - set(self.mgr._tool_to_provider), set(),
            'rejecting one bad name must not drop the legitimate tools',
        )
        self.assertEqual(len(self.mgr._tool_to_provider), 8,
                         'expected 8 routed tools, got %r' % sorted(self.mgr._tool_to_provider))

    def test_injection_into_agent_tool_surface(self):
        """agent_init.py:1401-1402 — the provider tools reach the agent, not just the manager.

        ``inject_memory_provider_tools`` reads ``agent._memory_manager`` and
        ``agent.tools``, then appends ``{"type": "function", "function": schema}``
        per tool. A stub missing the manager attribute makes it return 0 silently —
        exactly the "half on" failure mode it logs about — so the stub carries it.
        """
        from agent.memory_manager import inject_memory_provider_tools

        provider = load_memory_provider('memcore')
        self.mgr.add_provider(provider)

        class _StubAgent:
            valid_tool_names = None

            def __init__(self, manager):
                self._memory_manager = manager
                self.tools = []

        agent = _StubAgent(self.mgr)
        added = inject_memory_provider_tools(agent)
        names = {t['function']['name'] for t in agent.tools if 'function' in t}
        self.assertEqual(added, 8, 'inject added %d tools' % added)
        self.assertTrue(
            EXPECTED_TOOLS <= names,
            'injected tool surface missing %r (got %r)' % (EXPECTED_TOOLS - names, sorted(names)),
        )
        self.assertTrue(EXPECTED_TOOLS <= agent.valid_tool_names)
        # Idempotent: a second pass must not duplicate the schemas.
        self.assertEqual(inject_memory_provider_tools(agent), 0)
        self.assertEqual(len(agent.tools), 8)


if __name__ == '__main__':
    unittest.main()