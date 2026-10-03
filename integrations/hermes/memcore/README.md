# MemCore Hermes Integration

## 0.7.0 — Autonomy + Golden Rule (ADR-0018/0019/0020)

- Corroboration: same claim from 3 distinct agents auto-accepts
  (`source_backed`); 5 distinct agents crowns it Golden Rule
  (pinned+critical, always injected). Tombstone veto always wins.
- Auto-accept lanes: explicit durable signals ("จำไว้ว่า…",
  `memory_remember`, `memory_write/add`) and semantic `remember` with
  confidence ≥ 0.95 skip `candidate`. Assistant observations stay
  candidate-only.
- Recall: fingerprint dedup (corroborating copies collapse to one line) +
  word-boundary per-row cap (220 chars, Thai hard-cut fallback) instead of
  silently skipping oversized rows.
- Full UI removal: `dashboard/` + `desktop/` deleted. Operator surface is
  AI governed tools (`memory_*`) + `python -m memcore` CLI (`corroborate`,
  `golden-list`, `journal-sweep`, `decay`, `doctor`).
- Journal hygiene: stale builtin auto-dismiss (>7d), defer-cap auto-ignore
  (≥3 defers), freshness decay (current → aging → stale).

This directory is the **Git-tracked source of truth** for the MemCore Hermes plugin.
The copy under the local Hermes plugin directory is a deployed artifact and should
not be edited directly.

## Runtime layout

```text
memcore/
├── __init__.py              # registers MemCoreMemoryProvider
├── plugin.yaml              # Hermes plugin manifest (0.7.0)
├── native_provider.py       # journal-first native MemoryProvider adapter
├── plugin.py                # binding, governed tools, recall builder, auto-join
├── semantic_analyzer.py     # host-LLM semantic review adapter
└── tests/                   # integration regression suite
```

## Deploy / verify

From the MemCore repository root:

```powershell
# See what would change
python scripts/deploy_hermes_plugin.py --dry-run

# Deploy the allowlisted runtime files to local Hermes
python scripts/deploy_hermes_plugin.py

# Verify the installed copy is byte-for-byte in sync with Git
python scripts/deploy_hermes_plugin.py --check
```

The deployer copies only the explicit runtime allowlist. It does **not** copy tests,
`__pycache__`, or unknown files, and SHA-256 verifies every deployed file.
Use `--target <path>` or `MEMCORE_HERMES_PLUGIN_DIR` for a non-default install path.

## Engine resolution

The integration prefers an importable/installed `memcore` package. Development
fallbacks are resolved in this order:

1. importable `memcore` package
2. `MEMCORE_SRC` environment variable
3. repository-relative MemCore checkout
4. legacy `~/Workspace/memcore` checkout

The provider availability check is based on the loaded MemCore engine API, not a
machine-specific checkout path.

## Hermes configuration

```yaml
plugins:
  enabled: [memcore]
  entries:
    memcore:
      settings:
        store_path: ~/.memcore/memory.db
        agent_name: sora
        default_project: shared-memory
        auto_join: true
        path_bindings:
          - path: C:/work/project-a
            project: project-a
        inject:
          budget_chars: 1200
          max_items: 8
        semantic:
          auto_review:
            enabled: true
            max_events_per_turn: 1
            max_tokens: 256
            timeout_seconds: 30
            max_input_chars: 6000
            min_remember_confidence: 0.85
            failure_threshold: 2
            cooldown_seconds: 60

memory:
  provider: memcore
```

Identity and project scope come from configuration, never model-controlled tool
arguments. If the configured project or membership cannot be resolved, the plugin
fails closed.

`semantic.auto_review.enabled` is opt-in. When enabled, completed Hermes turns are
reviewed on Hermes' existing background memory-sync worker with the host-owned
`ctx.llm.complete_structured()` one-shot API. The reviewer never enters the agent
conversation/tool loop, processes at most `max_events_per_turn`, and only consumes
`semantic_review_required` events. A `defer` result is not automatically retried;
it remains pending for explicit/manual review. `remember` below
`min_remember_confidence` is downgraded to `defer`. Provider/model failures leave
the raw event unchanged and pending. After `failure_threshold` consecutive failures,
the local reviewer circuit opens for `cooldown_seconds`; turns continue normally but
skip semantic LLM calls until a half-open probe is allowed. This keeps a provider
outage from adding repeated latency or fallback traffic to every turn.

## Memory behavior

Hermes lifecycle activity is captured through the native provider:

```text
Hermes turn / memory write / delegation
              │
              ▼
       raw ingest journal
              │
      deterministic gate
              │
      ┌───────┴────────┐
      ▼                ▼
private candidate   semantic review
      │                │
      └──── governed MemCore ────► canonical recall
```

Raw journal rows are never recalled directly. Built-in Hermes add/replace/remove
writes are mirrored conservatively; replace/remove require exact MemCore provenance
and stay pending when the target cannot be proven.

### Governed tools

| Tool | Purpose |
|---|---|
| `memory_remember` | Store explicit durable project memory (accepted directly) |
| `memory_search` | Search canonical memory in the bound project |
| `memory_promote` | Promote an owned private memory to project scope |
| `memory_supersede` | Correct a memory while preserving version history |
| `memory_reject` | Reject and tombstone a claim |
| `memory_feedback` | Mark accepted/rejected/stale after use |
| `memory_review_queue` | Inspect this agent's pending semantic-review queue |
| `memory_review_decide` | Apply `remember`, `ignore`, or `defer` |

Trust lanes (ADR-0018/0019): explicit writes accept directly; semantic
`remember` with confidence ≥ 0.95 accepts directly; the same claim repeated
by 3 distinct agents auto-accepts project-wide (`source_backed`); 5 distinct
agents crowns it Golden Rule (pinned+critical). The analyzer still cannot
choose scope — corroboration is counted by the engine, never the model.

## Tests

Run this integration directly from its tracked source:

```powershell
cd integrations/hermes/memcore
python -m unittest discover -s tests -v
```

Current integration gate: **105 tests passing**.
