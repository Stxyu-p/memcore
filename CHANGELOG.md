# Changelog

All notable changes to MemCore are documented here.

## [0.8.0] - 2026-10-03

Capability release: recall quality is now measured, journal writes carry a
provenance seal, decay is reinforcement-aware, contradictions are scanned
(but only proposed, never auto-resolved), private scope subdivides without
touching governance, and memory versions are readable at a point in time.

### Added
- Recall-quality baseline (`harness/test_recall_baseline.py`): precision@k
  over fleet canonical facts in three tiers (exact/paraphrase/negation).
  Pinned: p@3 overall=0.62 (exact=1.00, paraphrase=0.50, negation=0.20).
  Any future retrieval change must move these numbers up, never down.
- HMAC-SHA256 provenance seal on journal writes (stdlib only, `store.py`);
  `doctor` reports sealed/valid/invalid counts.
- `recall_count` + `last_recalled` with reinforcement-aware decay: memories
  that proved useful resist freshness aging.
- Contradiction sweep (`memcore/contradiction.py`, lexical/substring-based,
  no new segmenter dep): `contradictions` scans read-only, `mark-conflict`
  is a separate governed step — propose, never auto-resolve.
- `scope_detail` subdivides the private scope; scope governance untouched.
- Bi-temporal point-in-time reads: `core.version_at()` + `history` CLI over
  `memory_version.valid_from/valid_until`.
- Corroboration funnel in `doctor`: fingerprint counts at sources
  1/2/accept(>=3)/golden(>=5) make the Golden Rule's reach visible.
- Zero-filled (externally damaged) stores are refused at open time instead
  of being treated as empty.

### Fixed
- Feedback-accept routes through `core.accept_memory`; tombstone veto kept
  (SORA audit F1 — audit bypass, not trust bypass; SORA's
  266/1247/corrupted-store numbers were stale).
- SORA audit F6 verified benign + config validation added.

### Validation
- Harness suite: 327 tests, OK (2 expected failures, pre-existing).
- Hermes integration suite: 98 tests, OK.
- `memcore doctor` exit 0 (journal health=ok, 282 events, 0 pending;
  6 snapshots, recovery_ready=True); plugin deploy `--check` in sync.

### Known / carried forward
- Corroboration still at_2=0 (88 fingerprints, all single-source; mika holds
  84 of 97 memories). Fleet write re-dispatch is the next step.
- SORA audit F2 (dual recall), F3 (dual connection lifecycle), P0 test gaps
  still open.

## [0.7.1] - 2026-10-03

Recovery-readiness release. MemCore had no backup mechanism for its entire
lifetime and `doctor` never checked for one; a zero-filled `memory.db`
discovered on 2026-10-03 lost roughly ten days of memories for want of any
recovery point. This release closes that gap and fixes the CLI defect that
made the incident harder to contain.

### Added
- `store.backup_store()` — snapshot via SQLite's online backup API
  (transaction-safe against concurrent writers, unlike a file copy), written
  to a temp file, `PRAGMA integrity_check`ed, then atomically moved into
  place. Retention keeps the newest `--keep` (default 14) managed snapshots.
- `store.verify_backups()` — content-free recovery-readiness report:
  snapshot count, newest/oldest age, `recovery_ready`, and specific problems.
  Only snapshots this module wrote (`<stem>-<YYYYmmddTHHMMSSZ>.db`) count, so
  a hand-placed `.bak` in the backup directory cannot make a store look
  recoverable.
- `memcore backup`, `memcore backup-status`, and
  `memcore restore-from-snapshot --snapshot <file> [--confirm]`. Restore
  previews without writing, preserves the current store as
  `<name>.pre-restore-<ts>.bak`, removes the replaced file's `-wal`/`-shm`
  sidecars, and aborts loudly if one of them is held by a live connection
  rather than grafting foreign WAL frames onto the restored image.
- `doctor` reports backup state and exits 1 when no verified snapshot exists
  or the newest is older than 7 days. Snapshot count is reported but does not
  gate: one integrity-checked snapshot is a usable recovery path.
- `harness/test_backup_restore.py` — 25 tests covering snapshot fidelity,
  self-containment, retention, foreign-file rejection, content-free reporting,
  restore preview/preserve/sidecar handling, and the doctor gate.

### Fixed
- `--db` is now honoured before *or* after the subcommand. Previously the flag
  was declared only on the top-level parser, so `memcore doctor --db X`
  silently fell back to `~/.memcore/memory.db` and wrote to the real user
  store. Every subparser now inherits a shared parent carrying `--db` with
  `default=argparse.SUPPRESS`, and `main()` resolves the fallback once.

### Validation
- Harness suite: 291 tests, OK (2 expected failures, pre-existing).
- Hermes integration suite: 96 tests, OK.
- `memcore doctor` OK; plugin deploy `--check` reports the deployed copy in
  sync with the Git source.

### Known
- The cause of the 2026-10-03 store corruption was never identified. Forensics
  showed surviving btree interior nodes with wiped leaf payloads, consistent
  with an interrupted bulk write rather than bit rot or a torn WAL checkpoint.
  No process in the agent log wrote the store during the relevant window.
  This release hardens recovery, not the (unknown) cause.

## [0.7.0] - 2026-10-03

Autonomy release: MemCore can now accept deliberate writes and earn trust from
independent corroboration without a human clicking accept, and the UI surface
is gone.

### Added
- Corroboration tiers with a Golden Rule (ADR-0018): 3 distinct `owner_agent_id`
  holding the same `claim_fingerprint` auto-accept the canonical copy
  (`scope='project'`, `verification='source_backed'`, never downgrading
  `user_authoritative`), and 5 distinct agents additionally pin it
  (`pinned=1, critical=1`) so it is injected in every recall block. Audited as
  `auto_corrob_accept` / `auto_golden_promote`; a tombstoned fingerprint vetoes
  both.
- Auto-accept entry points (ADR-0019): explicit `memory_remember` tool writes,
  deterministic "จำไว้ว่า…" signals, and semantic `remember` verdicts at
  confidence >= 0.95 create `lifecycle='accepted'` rows with
  `verification='unverified'` — one source is not two sources. `post_llm_call`
  stays candidate-only, so an assistant can never promote its own claims.
- `memcore` CLI surface for journal health, stats, Golden-rule listing,
  corroboration counts, and decay inspection.
- Architecture review `2026-10-03-autonomy-golden-rule.md`.
- Recall-quality and corroboration test suites.

### Removed
- Dashboard and desktop plugin surfaces (ADR-0020), including the dashboard
  plugin API, its 269-line test module, and the desktop plugin bundle. MemCore is
  consumed through `memory_*` tools and the CLI only.
- Cross-project showcase from the README.

### Fixed
- Delegation events are classified as operational, never as memory signals
  (`memcore/ingest.py`, `native_provider.py`).
- Semantic review routing and JSON compatibility restored on the Hermes
  integration.
- Recall injects only critical pins.
- Complete memory facts are preserved and Thai durable signals are recognized.

### Changed
- The Hermes plugin resolves the MemCore engine by probing `memcore.core`
  instead of `memcore`, so a PEP 420 namespace directory in the working
  directory can no longer shadow the real engine and silently disable the
  provider. See `harness/test_plugin_import_guard.py`.
- Migration gaps are no longer treated as a fully current schema.

### Validation
- Harness suite: 266 tests, OK (2 expected failures, pre-existing).
- Hermes integration suite: 92 tests, OK.
- `memcore doctor` OK; plugin deploy `--check` reports the deployed copy in sync
  with the Git source.
- Regression test `integrations/hermes/memcore/tests/test_dispatch_roundtrip.py`
  proves a `memory_remember` -> `memory_search` round trip never writes the real
  user store, with a negative control proving the leak is detectable.

## [0.6.1] - 2026-09-24

### Added
- MIT license matching the fleet's published projects.
- ADRs 0013-0017: critical-pins-only recall injection, standalone `test` as a
  trivial lane, no dispatch concurrency cap, five critical pins for fleet canon,
  and five auto-review events per turn.
- Mermaid dataflow diagram and a vector-memory comparison matrix in the README.

### Fixed
- Delegation events no longer register as memory signals.
- Semantic review routing and JSON compatibility restored.
- Recall injects only critical pins.
- Complete memory facts preserved; Thai durable signals recognized.
- One-off store maintenance scripts archived out of `scripts/`.

### Changed
- README re-synced to the published `Stxyu-p/memcore` repository.

## [0.6.0] - 2026-09-03

### Added
- Added migration `0012_unicode_fingerprint_repair` to repair legacy non-NFC claim fingerprints, tombstones, and fingerprint-derived idempotency aliases.
- Added migration `0013_current_version_ownership` with database triggers that prevent a memory from pointing at another memory's current version.
- Added doctor checks for current-version ownership drift, claim-fingerprint drift, and incomplete migration history.

### Changed
- Unicode recall now merges exact substring matches with FTS results instead of stopping after the first fast-path hit.
- Recall paths now fail closed on missing fingerprints, active tombstones, and cross-memory version pointers.
- Ingest event deduplication now hashes full source payloads and preserves uniqueness for long session IDs.
- Pending journal dismissal now requires the event owner or project owner and only permits review/deferred states.
- Hermes plugin connection pooling now evicts dead worker connections before thread-ID reuse can retain database locks.

### Fixed
- Prevented accepted duplicate claims from resurfacing after an equivalent claim is rejected or corrected.
- Prevented Unicode-equivalent claims from bypassing tombstone refusal guards after migration.
- Prevented raw/unclassified pending journal events from being dismissed before processing.
- Prevented cross-project content exposure through corrupted `current_version_id` pointers.
- Prevented migration gaps from being silently treated as a fully current schema.

### Validation
- Full test suite: 238 tests passed with 2 expected failures.
- Hermes deployed runtime verified byte-for-byte in sync with the Git source before release preparation.
