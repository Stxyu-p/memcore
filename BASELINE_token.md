# BASELINE — Token cost (memcore v0.8.8, 2026-10-14)

Source: measured firsthand this session (isolated tmp stores, never live ~/.memcore/memory.db).

## Read path today (FACT)
- `core.search/recall` returns FULL body per hit (no L0/L1/L2/L3 levels).
- Hermes injection caps at the render layer only:
  - `plugin.build_recall_block(pinned, hits, budget_chars=1200, max_items=8)`
  - per-row cap `MAX_ROW_CHARS=220` (word-boundary tail cut + ellipsis)
  - pinned share cap `PINNED_MAX_SHARE=0.5` when hits exist
  - `native_provider` defaults `_budget=1200, _max_items=8` (config `inject.*`)
  - `tool_memory_search` returns up to `limit=10` FULL-content rows (no truncation) — the expensive lane
- `memory_search` tool schema has no limit/level params (identity/scope params forbidden by design).

## Measured numbers (5 long facts ~869 chars each, query hit 5/5)
- HITS=5, FULL_CHARS=4345, L0_EST(180 chars/row)=900 → RATIO=4.83x
- AVG_FULL=869 chars (~217 tok @4ch/tok)
- `build_recall_block([], rows)` → BLOCK_CHARS=1080 (~270 tok) — render cap already saves ~4x vs raw
- Short-fact probe (72-char rows): FULL=72/row, no saving — L0 only pays on long bodies.

## Token math (rule of thumb @4 chars/token)
- Raw recall limit=10 x 869 chars ≈ 8690 chars ≈ 2170 tok per search call
- Via build_recall_block 1200-char cap ≈ 300 tok — already bounded for injection
- Unbounded lane = `tool_memory_search` (full content x10) — this is Gap 1's target.

## Target after Gap 1 (do not break)
- `memory_search(preview=True)` default: <= ~180 chars/hit (~45 tok), full body only on explicit `include_content=True`
- `build_recall_block` budget behaviour unchanged (existing TestRecallBlock green)
- Short rows pass through untouched (no padding)
