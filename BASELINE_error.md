# BASELINE — Errors / Gates / Config (memcore v0.8.8, 2026-10-14)

Source: `python -m memcore doctor` on live ~/.memcore/memory.db + `python -m harness` this session.

## Doctor (live store, firsthand 2026-10-14)
- integrity: ok; foreign-key violations: 0; journal_mode: wal (present=True)
- orphaned versions: 0; orphaned audit: 0; idempotency violations: 0
- current-version ownership: none; fingerprint violations: none
- tombstones: 41 active, 0 overridden; unguarded rejected: none; tombstone violations: none
- memberships: shared-platform ×6 (pchoke owner, mika/milim/nua/sora/altima member/profiles), x/x owner
- collisions: agent names none, project names none
- config bindings: default/altima/milim/nua/sora all OK
- network path: False; store parent writable: True
- fts index: in_sync=True
- corroboration: 116 fingerprint(s) (at_1=106, at_2=0, at_accept>=3=10, at_golden>=5=0)
- provenance: 784 checked (sealed=504 valid=504 invalid=0 unsealed=280)
- backups: 14 snapshots, newest 5.98d old, recovery_ready=True
- migration locks: none
- journal: health=operator_attention, events=784, semantic_pending=9, unresolved_builtin=1, failed=0, oldest_pending=2d
- plugin deploy check: OK (LOCALAPPDATA/hermes/plugins/memcore)

## Gates
- `python -m harness`: 527 tests OK (expected failures=2), ~132s — GREEN baseline
- `integrations/hermes/memcore tests.test_seed_canon_roundtrip`: covered by CI (host-independent lane)
- CI matrix: 3.13 + 3.14 (`python -m harness` + seed roundtrip) — local venv here is 3.13.15; 3.14 still UNKNOWN firsthand

## Config (FACT, stdlib-only)
- pyproject `dependencies=[]`, `requires-python>=3.10`, optional `local-embed=[fastembed]`
- store pragmas: WAL, busy_timeout 10000/5000, synchronous NORMAL, foreign_keys ON
- recall injection caps: budget_chars=1200, max_items=8, MAX_ROW_CHARS=220, PINNED_MAX_SHARE=0.5
- tool_memory_search: limit=10 FULL content (unbounded lane — Gap 1 target)

## Known operator TODO (not regressions)
- journal `operator_attention`: 9 semantic_pending + 1 unresolved_builtin — needs `journal-stats` + review/dismiss cadence, not code change
- backups newest ~6d (limit 7d) — run `memcore backup` before it gates doctor red

## Target after gap-fix (do not regress)
- doctor stays green except journal/backups age (operator cadence, not suite)
- harness 527 green; deploy-check OK; fts in_sync=True
