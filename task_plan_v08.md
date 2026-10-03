# MemCore v0.8 Development Plan

Lineage: continues the 2026-09-24 audit (`task_plan.md`, read-only, complete).
This is a new workstream with different constraints (active development allowed).

## Goal
Bring MemCore to production-ready and keep it there: recovery path guaranteed,
journal at health=ok, corroboration funnel visible and moving, recall quality
measured, and new capabilities added only behind owner-approved gates
(stdlib-only stays unless P Choke explicitly relaxes it; Thai support stays).

## Scope
- Repo: `C:/Users/BlankScreen/Workspace/memcore` (branch `main`, remote `Stxyu-p/memcore`).
- Live store: `~/.memcore/memory.db` (never write except through governed
  CLI/tools; `--db` first in tests).
- Constraints: stdlib-only unless owner approves a dep; no UI of any kind
  (owner 2026-10-03); no cron jobs (owner removed same day — snapshots stay
  manual); ADR-0018 thresholds unchanged until diversity exists; ADR-0019
  self-promotion block stays.

## Settled decisions (do not reopen without new evidence)
1. Agents write to shared memory (owner 2026-10-03). Standing order brief
   prepared at `cache/scratch/brief-fleet-write-order.txt`, not yet dispatched.
2. Threshold sweet spot = do NOT change N now (analysis 2026-10-03: N=2 would
   still fire 0 — all fingerprints at sources=1; all 54 evidence rows
   unverified). Revisit only when funnel shows sources≥2.
3. Thai stays. Thai is 16% of versions and owner-primary language; cost is
   ~150 LOC + 2 regexes (<3%). No PyThaiNLP unless owner approves a dep.
4. Observability = AI + CLI only (`doctor`, `stats`, `backup-status`, funnel line).
5. Root cause of 2026-10-03 zero-fill = UNKNOWN. Recovery hardened; cause not claimed.

## Phases
1. [done] Recovery hardening (0.7.1: backup/restore/doctor gate/`--db` fix; commit ac52fd9; pushed).
2. [done] Corroboration funnel visibility (commit 96072db; pushed) + journal cleared to health=ok + 4 candidates promoted.
3. [in_progress] Fleet write adoption — dispatch standing order to SORA/ALTIMA/MILIM (NUA already has research brief; gets write order after). Then measure funnel weekly via `doctor`.
4. [pending] SORA engineering audit return → fill roadmap §6; NUA research returned 2026-10-03 (§5 filled from out-nua2.log).
5. [pending] Recall quality baseline: measured query set (fleet canonical facts incl. negations) with precision@k. No retrieval change without a number first.
6. [pending] Capability builds, in order (each needs its own owner gate before code):
   6a. HMAC-SHA256 provenance on journal writes + retrieval-time ablation voting (~200 LOC, no deps).
   6b. recall_count + last_recalled + reinforcement-aware decay (~250 LOC).
   6c. Contradiction sweep, lexical/substring-based, NO new segmenter dep (~500 LOC; PyThaiNLP only if owner approves).
   6d. Scope enum extension (skill/episode/session) + scope-aware recall (~300 LOC).
   6e. Bi-temporal valid_from/valid_to + point-in-time recall (~600 LOC; schema migration — heaviest gate).
7. [pending] Per-phase gates: harness 295+ OK, integration 96+ OK, deploy `--check` OK, `doctor` exit 0, store row-count verified, then commit + push.

## Errors encountered (this workstream)
| Error | Attempt | Resolution |
|---|---|---|
| `--db` after subcommand silently hit real store; tests overwrote live DB twice (80→1) | 3 | Shared `common` parent parser with `default=argparse.SUPPRESS`; fallback resolved once in `main()`; regression tests added |
| patch tool repeatedly broke indentation in store.py/__main__.py | 3 | Rewrote regions via execute_code with py_compile check |
| `main(['--db', X, 'doctor'])` in tests ignored X | 1 | Same `--db` fix as above |
| `restore` name collided with existing subcommand | 1 | Renamed to `restore-from-snapshot` |
| verify_backups counted legacy .bak files as snapshots | 1 | Managed-snapshot filename regex + mtime ordering |
| min_count=3 gated a healthy single snapshot | 1 | Count is informational; only staleness gates |
| Test sidecar PermissionError WinError 32 | 2 | Checkpoint TRUNCATE + close before file replace; loud abort in restore |
| Old harness tests failed on new backup gate | 1 | Fixture stores get a snapshot so unrelated gate stays quiet |
| skill_manage description budget | 1 | Shortened description, detail in body |
| clarify prompts returned garbled on store-restore question | 1 | Re-asked minimal 3-choice; got explicit RESTORE |
| search_files regex `[ก-๛]` matched binary noise | 1 | Scoped to *.py + cross-checked with sqlite GLOB count instead |

## Outcome (so far)
- Commits pushed: ac52fd9, 4fa2137, 70e5663, 96072db, 9e506dd (tip).
- Live: 87 memories, journal health=ok (0 pending), 5 snapshots, doctor exit 0.
- Harness 295 OK; integration 98 OK (96 + 2 new F1 tests).
- NUA research returned 2026-10-03 (~16KB, out-nua2.log). SORA audit returned
  2026-10-03 (~20KB, out-sora2.log): F1 fixed in 9e506dd (severity corrected —
  audit bypass, not trust bypass; SORA's 266/1247/corrupted-store numbers were
  stale), F6 verified benign + config validation added. F2 (dual recall),
  F3 (dual connection lifecycle), P0 test gaps still open.
