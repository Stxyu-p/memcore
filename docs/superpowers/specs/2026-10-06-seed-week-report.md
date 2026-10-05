# Seed-week report — Day 1/7 (2026-10-06, content-free)

Source: live store `~/.memcore/memory.db` via read-only CLI (`stats`, `doctor`, `corroborate` dry-run). Counts only, no memory text.

## Autonomy (7-day window)
- `auto_accept`: 2 (2026-10-03)
- `auto_corrob_accept`: 35 (2026-10-05)
- `auto_golden_promote`: 0
- `duplicate-merge`: 0
- `contradiction-hold`: 0

## Funnel (`doctor` corroboration)
- fingerprints=89, at_1=79, at_2=0, at_accept>=3=10, at_golden>=5=0, reachable=true

## Journal (`doctor` + `stats`)
- health=review_pending, events=407 (ignored=355, pending=21, processed=31)
- semantic_pending=21 (deferred=4, review_required=17)
- unresolved_builtin=0, failed=0
- oldest_pending=2d (2026-10-03T15:23:40Z)

## Health gates
- `doctor` exit=0, recovery_ready=True (6 snapshots, newest 2.51d)
- fts in_sync=true, provenance invalid=0, bindings default/altima/milim/nua/sora OK
- recall baseline: 3 passed (p@3 guard intact)

## Acceptance gate #2 status
- `doctor` exit 0: PASS
- health=ok: NOT YET (review_pending, 21 semantic rows awaiting review)
- 0 pending older than 7d: PASS (oldest=2d)
- 7 consecutive days: Day 1/7 logged, 6 remaining

## Next
- Day 2 report due 2026-10-07. Semantic queue (21 rows) needs review/dismiss to reach health=ok — operator action, not auto.
