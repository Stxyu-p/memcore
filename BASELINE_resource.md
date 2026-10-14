# BASELINE — Machine resources (memcore v0.8.8, 2026-10-14)

Source: measured firsthand this session.

## Disk
- disk_total=127.0 GB, free=5.1 GB (4% free — tight; avoid heavy new deps)
- repo py_files=69, loc=25137 (excl .venv/__pycache__)

## Runtime deps (FACT: stdlib-only by design)
- `pyproject.toml`: `dependencies = []`, `requires-python = >=3.10`
- optional only: `local-embed = ["fastembed>=0.4.0"]` (never required for core/recall)
- `embedding.py` resolves providers 9router/openrouter/openai/ollama via env; fails closed to None → lexical FTS5 fallback
- Rule for this sweep: NO new dependencies (no pydantic, no typer, no portalocker).

## Concurrency primitives already present (FACT)
- SQLite WAL + `busy_timeout=10000` (open_store) / `5000` (hot path), `synchronous=NORMAL`, `foreign_keys=ON`
- Migration lock via `schema_migrations` row (`__migration_lock__`, TTL 30s, wait 10s) — cross-process safe boot
- Writes use `BEGIN IMMEDIATE` short transactions + audit rows
- Existing coverage: `test_concurrent_fresh_boots_do_not_interleave_migrations`, `test_concurrent_writes_no_corruption`, `test_multithreaded_concurrent_operations` — all green in 527 suite

## Target after gap-fix
- No new dep, no new daemon, no new server process
- Gap 3 verdict: stdlib advisory file lock ONLY if a real cross-process writer gap is demonstrated; SQLite WAL+IMMEDIATE already covers same-process + migration boot. Decision recorded in Gap 3 ledger below.
