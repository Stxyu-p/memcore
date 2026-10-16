# BASELINE — Token cost (memcore v0.8.8 + [Unreleased], 2026-10-16)

Source: measured firsthand this session (fresh probe, isolated tmp store, never live
~/.memcore/memory.db; provider=none, no network).

## Read path today (FACT — post preview lane)
- `core.search/recall` returns FULL body per hit; bounding happens at the render/
  tool layer only.
- Hermes injection caps:
  - `plugin.build_recall_block(pinned, hits, budget_chars=1200, max_items=8)`
  - per-row cap `MAX_ROW_CHARS=220` (salience-aware clause picker since d087c48-era)
  - pinned share cap `PINNED_MAX_SHARE=0.5` when hits exist
  - `native_provider` defaults `_budget=1200, _max_items=8` (config `inject.*`)
- Tool lane (`tool_memory_search`, limit=10): preview by default —
  `SEARCH_PREVIEW_CHARS=180` (clamped 40..500), full body only on explicit
  `include_content=True` (P1 preview is DONE; the old "unbounded lane" is closed).
- `memory_search` tool schema still carries no identity/scope params (design rule).

## Measured numbers (fresh probe: 5 long facts, avg 866 chars)
- HITS=5, FULL_CHARS=4331, AVG_FULL=866 chars (~217 tok @4ch/tok)
- `build_recall_block([], rows)` → BLOCK_CHARS=1097 (~274 tok) → ratio 3.95x
- Preview lane (180 chars): PREVIEW total 889 chars, avg 177/row, 5/5 truncated
  → ratio 4.87x full-to-preview; short rows pass through untouched (verified true)
- Prior freeze probe (2026-10-14): FULL 4345 / BLOCK 1080 / ratio 4.83x — matches
  within noise; the preview lane now gives the same ~4.8x saving on the tool path.

## Token math (rule of thumb @4 chars/token)
- Raw recall limit=10 x 866 chars ≈ 8660 chars ≈ 2165 tok per search call
- Via build_recall_block 1200-char cap ≈ ~275 tok — bounded for injection
- Tool lane default (preview 180 x 10) ≈ 1770 chars ≈ ~440 tok; full only explicit

## Target after Phase 1/2 (do not break)
- `memory_search` default preview ≤ ~180 chars/hit; `include_content=True` opt-in
- `build_recall_block` budget behaviour unchanged (existing TestRecallBlock green)
- Short rows pass through untouched (no padding)
