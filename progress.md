# MemCore audit progress

## 2026-09-24
- Loaded repository-review, debugging, plugin-management, verification, production-safety, and edge-case skills.
- Initial repository inventory and documentation read completed.
- Initial git-status command wrapper rejected foreground `notify/heartbeat`; no repository action occurred. Correct invocation will omit those flags.
- Phase 2 executed: harness 243 OK (2 expected failures), integration 104→105 OK, deploy check OK, doctor OK, benchmarks captured (scratch memcore-live-*.txt).
- Journal backlog cleared the same evening: 21 pending events decided (5 remember / 16 ignore+dismiss); journal-stats health ok, 0 pending, doctor hint gone.
- Delegation WIP fix verified (both suites + standalone regression test) and committed as 79a3e3a on explicit approval.
- Phase 3-5 closed: review written to docs/architecture-reviews/2026-09-24-memcore.md.

## Remaining (post-audit recommendations, not audit work)
- Weekly journal maintenance cadence (review R1).

## 2026-10-03
- Fixed the Hermes plugin engine-resolution bug: `_ensure_memcore_importable()` early-returned on `find_spec('memcore')`, which a plain `memcore/` directory in the CWD satisfies as a PEP 420 namespace package, so `Memory provider 'memcore' loaded but no provider instance found` (37 occurrences in agent.log). Now probes `find_spec('memcore.core')`; regression coverage in `harness/test_plugin_import_guard.py`. Both suites green, deployed copy verified in sync.
- Added `integrations/hermes/memcore/tests/test_dispatch_roundtrip.py` (6 tests) proving a `memory_remember` -> `memory_search` round trip through the real Hermes dispatch chain never writes the user's real store, with a negative control proving the leak is detectable. Written after an integration probe leaked one test row into production `~/.memcore/memory.db` (soft-deleted, no journal events, `doctor` OK).
- CHANGELOG 0.7.0 and 0.6.1 written from git history; every behavioural claim verified against the code (ADR-0018/0019 thresholds confirmed in `core.py`, not taken on the ADR's word).
- Doc drift fixed: integration README test count 105 -> 92 (review F3).
- L4 activation path now has permanent coverage: `integrations/hermes/memcore/tests/test_hermes_activation.py` drives Hermes' real init sequence (`load_memory_provider` -> `MemoryManager.add_provider` -> `inject_memory_provider_tools`, mirroring `agent/agent_init.py:1371-1402`) and asserts all 8 tools route and reach the agent tool surface, that a core-tool name collision is refused at the door without dropping the legitimate tools, and that injection is idempotent. Proven by mutation testing (4/4 seeded defects caught) — the first version of the collision test passed vacuously because `add_provider` filters shadowing names, so "no clash present" proved nothing.

## Closed
- Word-boundary per-row cap in `build_recall_block` (review F5) was already implemented in `plugin.py` (`MAX_ROW_CHARS = 220`, `_truncate_row_content`) with coverage in `harness/test_recall_quality.py` and `tests/test_plugin.py`. The "remaining" list was stale, not the code.

## 2026-10-03 — v0.8 closed
- Closing gate all green: harness 327 OK (2 expected failures, pre-existing), integration 98 OK, deploy `--check` OK, `doctor` exit 0 (journal health=ok, 286 events, 0 pending; 6 snapshots, recovery_ready=True).
- Shipped in 0.8.0: recall baseline p@3=0.62 (exact 1.00/paraphrase 0.50/negation 0.20), HMAC provenance seal, reinforcement-aware decay, contradiction sweep (propose-never-resolve), scope_detail, bi-temporal `version_at`, corroboration funnel, zero-filled-store refusal, feedback-accept via `core.accept_memory`.
- CHANGELOG 0.8.0 written; plugin.yaml 0.8.0; README gates updated to 327/98.
- Lesson: editing a deployed-tracked file (plugin.yaml) before `deploy` re-sync turns `doctor` red correctly — rule is `deploy` + `--check` BEFORE gate.
- Completed plans removed (`task_plan.md`, `task_plan_v08.md`); outcomes live here and in CHANGELOG.
- Carried forward: fleet write re-dispatch (funnel at_2=0, mika holds 84/97), SORA F2/F3/P0.
