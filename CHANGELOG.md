# Changelog

All notable changes to MemCore are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.8.5] - 2026-10-08

Local-first global memory: one command, no port, every agent.

### Added
- **`python -m memcore export`** writes governed memory to a file that coding
  agents already read, so MemCore can serve Codex, Antigravity, Claude Code and
  Freebuff with no daemon, no port, no MCP and no new dependency.
- **`--host` target selection** (`memcore/export.py:HOST_TARGETS`), each filename
  verified against the installed agent rather than assumed:
  - `codex` -> `MEMORY.md` (binary contains `MEMORY.md` x72, `AGENTS.md` x73)
  - `agy` -> `GEMINI.md` (binary contains `GEMINI.md` x49, `AGENTS.md` x54)
  - `freebuff` -> `.agents/memory.md` (its `--help`: *"Load this repository's
    `.agents` files and `mcp.json`"*)
  - `claude` -> `CLAUDE.md`
  - `all` -> every one, for repos where several agents work side by side
- **`--out-dir`** writes the host target(s) into any repo from anywhere.
- Content is identical across every target, verified by test. Divergent
  per-host files are the worst failure mode: two agents, two truths, no cause.

### Fixed
- **The export path imported `hermes_cli.config`**, which pulls in httpx, rich and
  asyncio — 3.7 seconds of wall time and an HTTP client on the import path of a
  daemonless offline engine. Measured with `-X importtime`. Replaced with a
  direct read of the memcore plugin settings from config.yaml: export dropped
  from **3,658ms to 485ms**, and `hermes_cli.profiles` was removed the same way
  (-82ms). `harness/test_export.py` now asserts no `httpx`/`requests`/`aiohttp`
  ever appears on that path.
- **Scope leak**: the first export reachable every private memory in the store
  (36 rows). Private is now opt-in via `--include-private`, and another agent's
  private rows stay hidden even then.
- **Idempotence**: a timestamp in the header made every run rewrite the file, so
  a repo would show a permanent dirty git status. The generated marker now
  carries a digest of the content, and a no-op re-run writes nothing.

### Added
- **CI** (`.github/workflows/ci.yml`) on Python 3.13 and 3.14: the engine harness
  plus the host-independent integration module. The Hermes adapter tests cannot
  run on a bare runner — that runtime is a separate repo and is not published in
  a form exposing the `RecallStatus` API the adapter subclasses — so CI names the
  one module that needs neither instead of discovering red builds.

### Changed
- `integrations/hermes/memcore/__init__.py` no longer imports the native provider
  at module load. That import needs the Hermes host runtime, so the package could
  not even be imported for engine-level tests or CI. Measured: with the host
  runtime made unimportable, the harness went from 455 tests / 1 error to
  **470 tests / 0 failures**.
- `scripts/`: deleted three dated one-off migrations (`cleanup_20260905`,
  `cleanup_phase2`, `shorten_20260913`) that hardcoded the live store path and
  would have destroyed real memory on a stray run. The three scratch-setup
  scripts now take their source path from `MEMCORE_SRC` instead of an absolute
  path baked in at authoring time.

### Fixed
- **README lied about its own numbers**: the badge and the verification block
  claimed 514 tests; the measured count was 470 + 94 = 564. Corrected, and the
  previously undocumented `memcore export` command added.

### Validation
- Harness: 470 tests OK. Export-specific: 19 tests, each verified to fail with
  its fix reverted.
- Hermes integration: 94 tests OK. `deploy_hermes_plugin.py --check`: OK.
- 4 host targets written and compared on disk: 4 files, **1 distinct body**.
- CI-shaped run, host runtime made unimportable: harness 470 OK; the
  host-independent integration module 4 OK.
- `memcore doctor`: exit 0. Live store: 133 rows before and after, FTS in sync.

## [0.8.4] - 2026-10-08

Production hardening pass: a recall bug found while measuring, latency work
measured rather than assumed, and a repo-wide audit.

### Fixed
- **The Thai substring lane buried every ranked hit** (correctness, not speed).
  `search()` returned the unranked `instr(content, term)` lane (bm25 rank 0.0)
  before the bm25-ranked FTS lane, and short-circuited on row count. Measured on
  the live store: `โทเคน Discord เก็บไว้ที่ไหน` returned 13 weak substring matches
  while the DISCORD_BOT_TOKEN fact — present in the store — never appeared.
  Ranked hits now come first and the substring lane only tops up the window.
  Probe recall 0.60 -> **0.80** with no probe regressing.
- The substring lane's early return is gone entirely; both lanes always merge,
  and the merge runs to the over-fetch depth so the collapse step still has
  spare rows to swap copies for distinct claims.

### Performance
- **The substring lane is now a fallback** (`_run_substring_lane`): it runs only
  when the ranked lane did not already fill the caller's window. Measured over
  13 realistic queries: 41.6ms -> **42.1ms total** with the ranked-first fix,
  and Thai queries 3-13ms -> **1.4-5.4ms** each. ASCII queries skip the lane
  entirely (previously it never ran for them; the guard is explicit now).
- `DISTINCT_OVERFETCH` 3 -> 2. Sweep on a live-store copy: factor 1 loses recall
  (mean overlap 0.86) and is *slower* (fewer rows to sort); factor 2 returns
  identical result sets on every probe; factor 3 buys nothing over 2.
- The freshness projection SQL and the substring lane are now shared, named
  fragments instead of duplicated inline SQL.

### Code quality (repo-wide audit, stdlib-only AST checks)
- No linter is installed and the project is stdlib-only, so the audit ran
  AST-based checks over all 55 files: 0 compile failures, 0 bare `except`,
  0 mutable defaults, 0 names shadowing builtins, 0 duplicate top-level defs,
  0 undefined-name candidates, 0 never-referenced top-level functions.
- Removed 6 unused imports (`math`, `sqlite3`, `hashlib`, `timedelta`, and two
  in scratch scripts). Every public `core` function already carries a docstring.
- `MAX_SUBSTR_TERMS` extracted as a named knob (was an inline `32` in two
  places) so the biggest Thai-latency lever is tunable without hunting.

### Added
- `harness/test_recall_invariants.py`: five assert-based checks in one place for
  the properties the rest of the suite assumes but never asserts together —
  scope and tombstone enforcement staying in SQL, `search()` working on a
  read-only handle without leaving a transaction open, duplicate-collapse being
  lossless, and the row shape being the documented nine-tuple. Runnable directly:
  `python -m harness.test_recall_invariants`.

### Validation
- Harness: 451 tests OK (2 expected failures).
- Hermes integration: 94 tests OK.
- `memcore doctor`: exit 0. `deploy_hermes_plugin.py --check`: OK.
- Recall baseline p@3 unchanged at 0.81.
- Latency on a copy of the live store, 10 realistic queries: prefetch 2.78ms
  average, search 1.78ms average, no block over budget, 78/80 distinct claims in
  8-slot windows, stored freshness labels untouched.
- **Independent review: two passes, five defects found and fixed.** A first
  review (ALTIMA via a separate agent session, High confidence) returned APPROVE
  with one minor finding. A second, adversarial review of the same diff returned
  **REQUEST CHANGES with two blockers and two majors**. The second pass is what
  shipped; the first pass's APPROVE did not catch these. All five are fixed here:
  1. *minor, first pass* — `pre_accept_conflict_check` returned a synthetic
     `('__empty_subject__', 'empty_subject_hold')` hit for content with no
     extractable subject, so every emoji-only, bare-digit, punctuation-only or
     stopword-only write became a permanent conflict. Measured before the fix:
     `😀🚀`, `12345` and `!!!` were all refused. The gate now fails open —
     there is nothing to contradict, and a silently held write is invisible.
  2. *blocker, second pass* — the contradiction gate's SQL prefilter was
     case-sensitive while `subject_key()` lowercases, so a capitalised rival row
     was invisible and **a contradicting claim was admitted as `accepted`**.
     Reproduced: `Gateway daemon port is 20128` stored vs `gateway daemon port is
     8080` written. The prefilter now case-folds both sides.
  3. *blocker, second pass* — removing the per-turn `conn.close()` left an open
     transaction on the cached handle whenever a write was interrupted by
     something `except Exception` cannot catch; the next turn then failed with
     "cannot start a transaction within a transaction". `_get_conn` now rolls
     back before handing a handle back, at the single choke point all four
     provider hooks route through.
  4. *major, second pass* — an idempotent retry of a held claim replayed the
     committed conflict row and returned OK, reporting success for a row that
     can never be recalled as accepted. The replay branch now re-raises.
  5. *major, second pass* — the substring-lane fallback counted rows, so
     corroborating copies filled the window and the collapse was left with a
     freed slot and nothing to put in it. The trigger now counts distinct
     claims.
  All eight regression tests in `harness/test_review_fixes.py` were verified to
  FAIL with each fix reverted, so none of them is vacuous.

## [0.8.3] - 2026-10-07

Recall-window diversity and honest trust labels.

### Fixed
- **Result windows filled with copies of one fact** (`_collapse_duplicate_claims`):
  the fleet corroborates by re-writing the same claim from several agents, so one
  true fact arrives as N identical rows. `search` now returns
  `claim_fingerprint` and folds duplicate copies behind the canonical
  (best-ranked) one, and reads `DISTINCT_OVERFETCH` (3) times deeper so the
  freed slots fill with other claims. Folding is stable — no claim is ever
  lost, and a copy is only ever dropped from the window by the caller's `limit`.
  Measured on the live store: `HERMES_PROFILE` returned 4 rows carrying 1
  distinct claim; across 10 realistic queries an 8-slot window now carries
  **79/80 distinct claims** (was 45/64).
- **The freshness label in a recall line could lie**: `apply_freshness_decay`
  is a manual sweep (no cron by owner decision), so every stored label sat at
  `current` while rows were weeks old, and the recall line claimed `current`
  for facts decay would have aged. The label is now projected inside the same
  SQL pass — age-based, with recent recall rescuing a row exactly as the
  durable sweep does, and a real sweep's stored value still winning. Nothing is
  written: the store still reads 133 `current` after searching.

### Changed
- `GOLDEN_N = 5` is unreachable on this fleet (the owner is a human and wrote 2
  memories, so no claim can reach 5 distinct writers). Measured that lowering it
  to 4 changes recall output by **zero bytes** — `build_recall_block`'s
  `PINNED_MAX_SHARE` already caps the pinned tier. Left at 5 with a `ponytail:`
  note rather than changing a threshold that provably does nothing.

### Validation
- Harness: 437 tests OK (2 expected failures).
- Hermes integration: 94 tests OK.
- `memcore doctor`: exit 0.
- Recall baseline p@3 unchanged at 0.81.
- Prefetch p50 1.3ms; search p50 0.6-0.8ms; no block over budget.

## [0.8.2] - 2026-10-07

Recall-quality and write-governance hardening pass, plus fleet-wide auto-learn.

### Fixed
- **Recall block was pinned-starved (root cause of ranking having no effect)**:
  `build_recall_block` now caps the pinned tier at `PINNED_MAX_SHARE` (0.5) of
  the budget when the query produced search hits. Before, five long Golden pins
  consumed an entire 1200-char block and **zero** search hits ever rendered.
  Measured on the live store over 10 realistic queries: pinned lines 40 -> 20,
  search-hit lines **0 -> 30**.
- **`memory_remember` bypassed the contradiction gate**: `core.create_memory`
  now runs `pre_accept_conflict_check` whenever it would create an `accepted`
  row, so all four write lanes (explicit tool, ingest, semantic, corroboration)
  pass through one choke point. A contradicting claim is still created — as
  `conflict`, paired with the memory it disagrees with and audited
  `contradiction-hold` — then `core.ContradictionHold` (carrying `.memory_id`)
  is raised instead of silently accepting a disagreement.
- **`pre_accept_conflict_check` was 34ms per write** (Python-tested every live
  row). Now SQL-prefiltered on the first subject token; strictly narrowing
  because a subject key is built from the content's own words. 34.7ms -> 3.3ms,
  results verified identical across all 123 live rows plus synthetic
  contradiction pairs.

### Performance
- The provider no longer opens a WAL store per turn. `prefetch`, `sync_turn`,
  `on_memory_write`, and `on_delegation` reuse the existing per-thread
  connection cache (`agent_plugin._get_conn`). Opening a writable handle cost
  ~4.6ms, of which ~3.9ms was `PRAGMA synchronous = NORMAL`. Measured:
  prefetch 16.2ms -> 2.5ms, sync_turn 11.3ms -> 0.24ms.
- Numeric-mismatch demotion in recall ordering: a query naming a concrete value
  ("port 8080", "page size 100") no longer leads with a memory stating a
  different value. Lexical recall has no polarity, but both sides carry
  standalone numbers, so disjoint number sets are the usable negation signal.
  Only reorders rows it actually demotes, and only when the query has numbers.

### Added
- `semantic.auto_review` is now enabled for the ALTIMA, MILIM, NUA, and SORA
  profiles (was MIKA-only), so all five agents can drain their own semantic
  review backlog. Workers use `max_events_per_turn: 2` (MIKA keeps 5) because
  their turns are longer; the existing circuit breaker, cooldown, and
  min-confidence gates are unchanged.

### Removed
- `plugin.pre_llm_call` / `plugin.post_llm_call`: module-level functions that
  Hermes never registered (`register()` only calls `ctx.register_memory_provider`)
  and that the live hook registry confirms were never invoked. ~100 unreachable
  lines plus their 12 tests; the one unique test was ported to the provider
  suite. Net: code lighter and recall behaviour unchanged.

### Validation
- Harness: 422 tests OK (2 expected failures).
- Hermes integration: 94 tests OK.
- `memcore doctor`: exit 0 (integrity ok, FTS in sync, backups healthy).
- Recall baseline p@3 unchanged at 0.81 (exact 1.00, paraphrase 1.00,
  negation 0.20) — no regression; the negation work is guarded by its own
  tests rather than by that fixture's cross-language subject keys.

## [0.8.1] - 2026-10-07

Hardening, recall quality, and performance release: Recall 2.0 with Thai bigram de-gluing and fleet alias expansion, continuous decay/salience ranking with Ebbinghaus forgetting curves, pre-emptive rejection guards with duplicate sweeping, edge-case sadist adversarial robustness suite, and Ponytail hot-path performance optimizations.

### Added
- **Recall 2.0 Engine (Lanes 3.1 & 3.2)**:
  - Thai character bigram de-gluing (`_thai_bigrams`): generates character 2-gram prefix terms for unspaced Thai queries, bridging SQLite `unicode61` tokenizer limitations without external dependencies.
  - Fleet alias expansion map (`_expand_aliases`): query-side alias resolution mapping colloquial references to canonical fleet terms (`'AI gateway' -> '9router'`, `'ทีม' -> 'fleet roster'`, `'พี่โชค' -> 'thai'`, `'สแกน' -> 'scan pacing'`).
  - Unicode substring fallback (`instr` OR-clauses) for non-ASCII queries merged with FTS5 BM25 hits.
  - Recall precision floor elevated: p@3 overall improved to **0.81** (exact=1.00, paraphrase=1.00, negation=0.20), substantially exceeding the 0.62 baseline floor.
- **Continuous Retention & Decay Ranking (Adopt-1)**:
  - Mathematical retention scoring in SQL ORDER BY: `salience * exp(-lambda * age) + sigma * ln(1 + recall_count) * exp(-mu * days_since_access)`.
  - Type-aware Ebbinghaus half-life decay (`fact` 60d, `decision` 90d, `preference` 90d, `note` 30d, `observation` 14d).
  - Recency reinforcement (`record_recall`): memories recalled within 14 days resist freshness decay based on historical usage frequency.
  - Ablation experimental flags: `MEMCORE_ABLATE_DECAY`, `MEMCORE_ABLATE_THAI_BIGRAM`, `MEMCORE_ABLATE_ALIAS_EXPANSION`, and `MEMCORE_FAKE_NOW` simulated clock for deterministic testing.
- **Rejection Hardening & Refusal Guards (Adopt-2)**:
  - Pre-emptive `reject_value`: files project-wide refusal tombstones without requiring an existing memory row.
  - Atomic live duplicate sweep: rejecting one claim sweeps all active same-claim duplicates in the same transaction.
  - Soft-override `unreject_tombstone`: re-opens admission by exact tombstone ID or unique fingerprint prefix without resurrecting purged history.
- **Edge-Case Sadist Adversarial Suite (`harness/test_adversarial_sadist.py`)**:
  - 15 comprehensive torture tests across 5 taxonomies (primitive/nullity, numeric boundaries, chrono/decay shifts, collection isolation, and multithreaded WAL concurrency).

### Fixed
- **Surrogate Character Crash**: Handled lone surrogate characters (`\ud800`) in `fingerprint()`, `create_memory()`, `supersede()`, and `reject_value()` by sanitizing with `errors='replace'` and failing closed with typed `MemCoreError`, preventing unhandled Python `UnicodeEncodeError` crashes.
- **`record_recall` NoneType Crash**: Added short-circuit guard `if not memory_ids: return 0` in `record_recall()`, upholding the "never raises" contract when passed `None`.
- **Negative Aging Days**: `apply_freshness_decay()` now validates `aging_days >= 0` instead of silently producing SQLite `NULL` evaluations.

### Performance (Ponytail Hot-Path Optimizations)
- Connection-level column caching (`_has_recall_cols`) eliminates redundant `PRAGMA table_info(memory)` executions on hot search and recall queries.
- Precomputed static SQL fragments (`_DECAY_LAMBDA_SQL_M`, `_DECAY_SALIENCE_SQL_M`) eliminate per-query dictionary sorting and string formatting overhead.
- Lifted dynamic inline imports to module level, eliminating thousands of `importlib` resolutions during search.
- Search throughput reaches ~850-950 req/s (~1ms latency) with zero external caching infrastructure.

### Validation
- Harness test suite: 412 tests, OK (2 expected failures, pre-existing).
- Hermes integration suite: 102 tests, OK.
- Total test count: 514 tests passing.
- `memcore doctor`: exit 0 (integrity ok, 0 FK violations, FTS in sync, backups healthy).
- Recall baseline p@3: 0.81 (overall=0.81, exact=1.00, paraphrase=1.00, negation=0.20).

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
