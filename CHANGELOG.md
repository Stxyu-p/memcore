# Changelog

All notable changes to MemCore are documented here.

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
