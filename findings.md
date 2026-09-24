# MemCore audit findings

## Status
Working notes. Facts must be tied to a command, file path, or runtime observation. Do not treat README claims as verification.

## Repository orientation
- Source root: `C:/Users/BlankScreen/Workspace/memcore`
- Core modules: `memcore/core.py`, `memcore/ingest.py`, `memcore/semantic.py`, `memcore/store.py`, `memcore/__main__.py`
- Hermes source: `integrations/hermes/memcore/`
- Tests: `harness/`, `integrations/hermes/memcore/tests/`
- Schema: `schema/schema.sql`
- Deployment: `scripts/deploy_hermes_plugin.py`
- Repository README claims v0.6.0, 243-test gate, 2 expected failures; integration README claims 83 tests, while source manifest says 0.6.1. These are documentation/test-count claims requiring fresh execution.

## Confirmed source facts
- Plugin is declared as `kind: exclusive`, version 0.6.1 in `integrations/hermes/memcore/plugin.yaml`.
- Plugin source contains native provider, plugin adapter, semantic analyzer, dashboard API, desktop UI, and integration tests.
- `plugin.py` binds identity/project from config, exposes governed memory tools, performs bounded recall, and journals post-LLM observations.
- Recall uses pinned+critical rows plus FTS hits, budget-capped, with whole-item skipping.
- `post_llm_call` writes private observations for assistant text >=80 chars, capped at 2000 chars; comments say candidate-only.
- README says raw journal is never recalled directly and semantic `remember` can only create private candidates.
- The integration README documents auto-review at up to 5 events/turn, but says no automatic acceptance/verification and no live model reliability proof.

## Open questions / evidence needed
- Actual test counts, failures, warnings, and runtime duration.
- Whether deployed plugin is byte-for-byte current and runtime tools are actually registered.
- Whether the live store is healthy, backlog size, recall latency, duplicate/idempotency behavior.
- Whether native provider path and legacy plugin path have parity and whether the two hook surfaces can double-write.
- Whether FTS5 alone is sufficient for Thai/Unicode/semantic queries and how ranking behaves.
- Whether dashboard routes are useful, secured, and reachable in current Hermes runtime.

## Audit log
- Created `task_plan.md` for a read-only audit.
