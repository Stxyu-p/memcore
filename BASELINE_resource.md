# BASELINE — Machine resources (memcore v0.8.8 + [Unreleased], 2026-10-16)

Source: measured firsthand this session (post-sweep refresh).

## Disk
- disk_total=119 GB, used=103 GB, free=16 GB (87% used)
- repo live footprint: memcore/ 10 modules (8,740 LOC), integrations/hermes/memcore/
  4 runtime modules (1,611 LOC) + 6 test modules (2,091 LOC), harness/ 39 files
  (11,142 LOC), scripts/ 7 files (558 LOC); repo py total 68 files, 24,630 LOC
  (excl .planning/, .venv/, __pycache__). .planning/memcore-sweep/ (935 LOC) still
  present at measurement time — Phase 0 removes it.

## Runtime deps (FACT: stdlib-only by design)
- `pyproject.toml`: `dependencies = []`, `requires-python = >=3.10`
- optional only: `local-embed = ["fastembed>=0.4.0"]` (never required for core/recall)
- `embedding.py` resolves providers 9router/openrouter/openai/ollama via env; fails
  closed to None → lexical FTS5 fallback
- Rule for this round: NO new dependencies, no new daemons/processes.

## Concurrency primitives already present (FACT)
- SQLite WAL + `busy_timeout=10000` (open_store) / `5000` (hot path),
  `synchronous=NORMAL`, `foreign_keys=ON`
- Migration lock via `schema_migrations` row (`__migration_lock__`, TTL 30s, wait
  10s) — cross-process safe boot
- Writes use `BEGIN IMMEDIATE` short transactions + audit rows
- Existing coverage: `test_concurrent_fresh_boots_do_not_interleave_migrations`,
  `test_concurrent_writes_no_corruption`, `test_multithreaded_concurrent_operations`
  — all green in the 543-test suite

## Timing facts (resource view)
- `python -m harness`: 543 tests in 56.36 s (fresh; prior freeze 527 in 131.9 s)
- integrations suite: 97 tests in 6.196 s
- cold `import memcore.core`: median ≈ 0.15 s (3 runs: 0.1498/0.1537/0.1715)

## Target after Phase 1/2 (do not regress)
- No new dep, no new daemon, no new server process
- Production LOC net ≥ 280 lower (Phase 1 + Phase 2 scope)
- Harness wall time stays in the same order of magnitude; suites stay green
