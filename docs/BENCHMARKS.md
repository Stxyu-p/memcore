# MemCore Benchmarks & Baselines

Historical baselines measured on live environment and test harness (October 2026).

---

## 1. Errors, Gates & Health Baseline

### Doctor (live store, exit 0)
- **Integrity**: ok; foreign-key violations: 0; journal mode: wal (file present=True)
- **Orphaned entities**: versions: 0; audit: 0; idempotency violations: 0
- **Violations**: current-version ownership violations: none; memory fingerprint violations: none
- **Tombstones**: 45 active, 0 overridden; unguarded rejected: none; tombstone violations: none
- **Memberships**: shared-platform ×6 (pchoke owner, mika/milim/nua/sora/altima members)
- **Collisions**: agent names none, project names none
- **FTS index**: in_sync=True
- **Provenance**: 886 checked (sealed=606 valid=606 invalid=0 unsealed=280)
- **Backups**: 14 snapshots, newest 0.0d old, recovery_ready=True

### Test Harness & Gates
- `harness`: 605 tests OK (expected failures=2) — GREEN
- `integrations` (hermes-agent): 97 tests OK — GREEN
- Recall p@3: overall=0.86 (exact=1.00, negation=0.40, paraphrase=1.00)

---

## 2. Latency Baseline

### Environment
- Python: 3.13 / 3.12 (AMD64)
- SQLite: journal_mode=WAL, busy_timeout=10000 (open_store) / 5000 (hot path)
- Platform: Windows 11

### Search Latency (scripts/bench_search.py, 500 seeded rows, 1000 calls)
- p50: ~2.42 - 3.50 ms
- p95: ~4.57 - 6.11 ms
- min: ~1.85 - 1.99 ms

### Hot Paths (scripts/bench_hot_paths.py)
- Private claim lookup (2000 memories, indexed): p50 ~0.017 ms, p95 ~0.063 ms
- Read-only store open: p50 ~0.50 - 0.54 ms, p95 ~0.92 - 1.01 ms
- Import preview (2000 existing, 100 input): p50 ~5.88 ms

---

## 3. Resource Baseline

- SQLite WAL + `busy_timeout=10000` (open_store) / `5000` (hot path), `synchronous=NORMAL`, `foreign_keys=ON`
- Migration lock via `schema_migrations` row (`__migration_lock__`, TTL 30s, wait 10s) — cross-process safe boot
- Cold `import memcore.core`: median ≈ 0.15 s

---

## 4. Token Cost Baseline

- Hermes injection caps:
  - `plugin.build_recall_block(pinned, hits, budget_chars=1200, max_items=8)`
  - per-row cap `MAX_ROW_CHARS=220` (salience-aware clause picker)
  - pinned share cap `PINNED_MAX_SHARE=0.5` when hits exist
- Tool lane (`memory_search`, limit=10): preview by default — `SEARCH_PREVIEW_CHARS=180` (clamped 40..500), full body only on explicit `include_content=True`
- Token savings ratio: ~4.87x full-to-preview reduction
