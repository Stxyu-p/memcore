# BASELINE — Errors / Gates / Config (memcore v0.8.8 + [Unreleased], 2026-10-16)

Source: `python -m memcore doctor` on live ~/.memcore/memory.db (post backup +
journal-sweep) + `python -m harness` this session.

## Doctor (live store, firsthand 2026-10-16, exit 0)
- integrity: ok; foreign-key violations: 0; journal mode: wal (file present=True)
- orphaned versions: 0; orphaned audit: 0; idempotency violations: 0
- current-version ownership violations: none; memory fingerprint violations: none
- tombstones: 45 active, 0 overridden; unguarded rejected: none; tombstone violations: none
- memberships: shared-platform ×6 (pchoke owner, mika/milim/nua/sora/altima members), x/x owner
- collisions: agent names none, project names none
- config bindings (root-level view): default / altima / milim / nua / sora all OK
  (NB: when `HERMES_HOME` is scoped to a single profile, the probe reports that
  profile as 'default' only — environment artifact, both views exit 0)
- network path: False; store parent writable: True
- fts index: in_sync=True
- corroboration: 145 fingerprint(s) (at_1=135, at_2=0, at_accept>=3=10, at_golden>=5=0)
- provenance: 886 checked (sealed=606 valid=606 invalid=0 unsealed=280)
- backups: 14 snapshots, newest 0.0d old (fresh backup this session), recovery_ready=True
- migration locks: none
- journal: health=review_pending, events=886, semantic_pending=13,
  unresolved_builtin=0, failed=0; oldest_pending=4d
- plugin deploy check: OK (LOCALAPPDATA/hermes/profiles/sora/plugins/memcore)
- Sweep evidence: pre-sweep state was unresolved_builtin=4; `journal-sweep
  --builtin-days 0 --apply` dismissed 4 events (evt-5bb5bb360c389ac678a91220,
  evt-a491f1a17311b79c8869d8eb, evt-36cfaaebfdf957692d70b016,
  evt-cef6c1d34b048ebbbaf7981f) → health operator_attention → review_pending.

## Gates
- `python -m harness`: 543 tests OK (expected failures=2) in 56.36 s — GREEN
- integrations suite (hermes-agent python): `Ran 97 tests ... OK` in 6.196 s — GREEN
- recall p@3: overall=0.86 (exact=1.00, negation=0.40, paraphrase=1.00)

## Config (FACT, stdlib-only)
- pyproject `dependencies=[]`, `requires-python>=3.10`, optional `local-embed=[fastembed]`
- store pragmas: WAL, busy_timeout 10000/5000, synchronous NORMAL, foreign_keys ON
- recall injection caps: budget_chars=1200, max_items=8, MAX_ROW_CHARS=220, PINNED_MAX_SHARE=0.5
- tool_memory_search: limit=10, preview 180 chars default, full only via include_content=True

## Known operator TODO (not regressions)
- journal `review_pending`: 13 semantic_pending events await review/dismiss cadence
  (`memcore journal-review-list` / `journal-dismiss`) — operator cadence, not code.
- Disk: 16 GB free — keep dist/build artifacts pruned (Phase 0 does).

## Target after Phase 1/2 (do not regress)
- doctor exit 0; zero violations; fts in_sync=True; recovery_ready=True;
  journal health stays ok/review_pending with unresolved_builtin=0
- harness 543 + integrations 97 remain green; deploy-check OK
