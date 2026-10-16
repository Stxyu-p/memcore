# BASELINE — Latency (memcore v0.8.8 + [Unreleased], 2026-10-16)

Source: measured firsthand this session on P Choke's machine (post-sweep refresh;
supersedes the 2026-10-14 freeze).
Measurement protocol: benches re-run 3x this session; the box is shared with the
live agent fleet, so run-to-run jitter is real and is reported (see "Jitter").
Do NOT treat remote claims (LongMemEval %, ScanGit #) as facts — they are UNKNOWN
until reproduced here.

## Environment
- python: 3.13.15 (MSC v.1944 AMD64) via `.venv/Scripts/python.exe`
- platform: Windows-11-10.0.22631-SP0, cpu_count=4
- sqlite: journal_mode=WAL, busy_timeout=10000 (open_store) / 5000 (hot path)
- repo: `C:/Users/BlankScreen/Workspace/memcore`, branch `main`, HEAD `d087c48`
- disk: 119G total, 103G used, 16G free (87% used)

## Search latency (scripts/bench_search.py, 500 seeded rows, 1000 calls)
- p50: 2.42 / 3.50 / 4.17 ms (3 runs, same session)
- p95: 4.57 / 6.11 / 9.91 ms
- min: 1.85 / 1.93 / 1.99 ms
- max: 17.57 / 20.60 / 48.43 ms
- Command: `.venv/Scripts/python.exe scripts/bench_search.py`
- Previous freeze (2026-10-14): p50 4.12 / p95 8.75 / max 52.80. The d087c48
  substring short-circuit is still visible in the best runs; the spread above is
  load, not code (min is stable ≈1.85–1.99 every run; one outlier run sat at
  p50 4.17 while min stayed 1.99).

## Hot paths (scripts/bench_hot_paths.py)
- private claim lookup (2000 memories, indexed): p50 0.0173 / 0.0168 / 0.0291 ms,
  p95 0.0630 / 0.0194 / 0.1283 ms; legacy scan p50 ≈ 6.86–7.99 ms (≈245–460x)
- read-only store open: p50 0.5462 / 0.4996 / 0.4972 ms, p95 1.0136 / 0.9320 /
  0.9227 ms; checked open p50 ≈ 2.5–3.4 ms (≈5–6x)
- Command: `.venv/Scripts/python.exe scripts/bench_hot_paths.py`

## Import preview (scripts/bench_import_plan.py, 2000 existing, 100 input)
- p50: 5.88 ms (min 5.69 / max 7.39), would_add 80 / skipped 20
- Command: `.venv/Scripts/python.exe scripts/bench_import_plan.py`

## Cold import
- `import memcore.core`: 0.1498 / 0.1537 / 0.1715 s (median ≈ 0.15 s)

## Suite time / recall quality
- `python -m harness`: 543 tests OK (expected failures=2) in 56.36 s
- integrations suite (hermes-agent python): 97 tests OK in 6.196 s
- recall p@3 (`harness.test_recall_baseline`): overall=0.86 exact=1.00
  negation=0.40 paraphrase=1.00

## Jitter (FACT)
The machine runs the live fleet; single bench runs vary up to ~2x on p50 while
`min` stays flat. Regression decisions must compare same-session interleaved
runs (median-of-runs), never one run against one number. The Phase-1 gate value
"p50 ≤ 2.40 ms" was recorded on a quieter machine; treat it as the code-quality
target and the same-session before/after comparison as the regression bar.

## Target after Phase 1/2 (do not regress)
- bench_search: keep min ≤ ~2.0 ms and median-of-runs p50 not worse than the
  values above in the same session; aim ≤ 2.40 ms on a quiet run
- hot paths: claim lookup p50 within ±5% jitter of 0.0166 ms best-case; read-only
  open p50 within jitter of ~0.50 ms
- harness 543 + integrations 97 remain green; recall p@3 must not drop
