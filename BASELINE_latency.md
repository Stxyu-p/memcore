# BASELINE — Latency (memcore v0.8.8, 2026-10-14)

Source: measured firsthand this session on P Choke's machine.
Do NOT treat remote claims (LongMemEval %, ScanGit #) as facts — they are UNKNOWN until reproduced here.

## Environment
- python: 3.13.15 (MSC v.1944 AMD64) via `.venv/Scripts/python.exe`
- platform: Windows-11-10.0.22631-SP0, cpu_count=4
- sqlite: 3.53.1, journal_mode=WAL, busy_timeout=10000 (open_store) / 5000 (hot path)
- repo: `C:/Users/BlankScreen/Workspace/memcore`, branch `main`, HEAD `077f45a`
- py_files=69 (excl .venv/__pycache__), loc=25137

## Search latency (scripts/bench_search.py, 500 seeded rows, 1000 calls)
- p50: 4.12 ms
- p95: 8.75 ms
- min: 1.83 ms / max: 52.80 ms
- Command: `.venv/Scripts/python.exe scripts/bench_search.py`
- Note: Thai glued queries cost ~5ms vs ASCII ~1ms (see core.py MAX_SUBSTR_TERMS comment).

## Suite time
- `python -m harness`: 527 tests OK (expected failures=2) in 131.9s
- hybrid recall p@3 overall=0.86; baseline p@3 overall=0.86 (exact=1.00 negation=0.40 paraphrase=1.00)

## Target after gap-fix (do not regress)
- p50 <= 5ms, p95 <= 12ms on same bench
- harness still 527 green
