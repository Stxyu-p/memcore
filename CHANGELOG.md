# Changelog

All notable changes to MemCore are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.5.0] - 2026-10-29

### Added
- **Terminal Injection Guard (`_TAINTED_CMD_RE`):**
  - Closes the critical security gap where indirect prompt injection commands (`curl`, `wget`, `git clone`, `Invoke-WebRequest`, `iwr`) executed via `terminal` previously escaped untrusted tool detection. All external network fetches now stamp `tainted: True`, vetoing auto-accept lanes and forcing memories to remain strictly quarantined as `candidate`.
- **Trimtok Live Telemetry Integration:**
  - `_event_metadata` now automatically queries and attaches live Trimtok compression metrics (`trimtok v5.X usage: ... events, ... folds, ... chars saved`) into MemCore turn events and audit journals.
- **Idempotent Migration Runner:**
  - Migration execution handles duplicate column errors gracefully, preventing operational failures when replaying migrations against partially upgraded schemas.

## [2.4.0] - 2026-10-29

### Added
- **Tainted Context Guard against Indirect Prompt Injection:**
  - Untrusted I/O tools (`web_search`, `web_extract`, `browser_exec`) automatically veto auto-accept lanes, preventing malicious webpage instructions from injecting unverified facts directly into memory.
- **Official Simplified Vector Brand Logo Assets.**

## [2.3.0] - 2026-10-28

### Added
- **Governed Knowledge Triples (`memory_triple`)**:
  - Schema migration `0019_memory_triples` adding `memory_triple` table with foreign keys cascading from `memory` and `memory_version`.
  - Stored via `create_memory(..., triples=[...])` and `supersede(..., triples=[...])`.
  - Queryable via `query_triples(conn, project_id, subject, predicate, object, active_only=True)` and MCP tool `memory_triple_query`.
  - Inherits strict bitemporal governance: superseded or tombstoned claims automatically invalidate attached triples.
- **Governed Memory Compaction (`memcore compact`)**:
  - Lossless consolidation of accepted historical memories (`older_than_days`, default 14) into bitemporally superseded decision snapshots.
  - CLI subcommand `memcore compact --project <name> [--days 14] [--dry-run]`.
  - Prunes prompt token bloat while keeping complete audit trail and bitemporal history intact.
- **Two-Stage 1-Bit Binary Vector Quantization**:
  - Schema migration `0020_binary_vector` adding 48-byte sign-bit representations for 384-dimensional `multilingual-e5-small` embeddings.
  - In-process microsecond Stage-1 candidate filtering using bitwise Hamming POPCOUNT (`pack_binary_vector`, `hamming_distance`).
  - Stage-2 exact float32 cosine similarity reranking on surviving candidates, delivering up to 10x retrieval throughput boost.
- **Type-Specific Retention Policies & Constraint Immunity**:
  - Enforced zero decay ($\lambda = 0.0$) and high salience (1.6x) for `constraint` memories ("never...", "ห้าม...").
  - Rapid decay (3-day half-life, salience 0.7x) for transient `note` memories, prioritizing them for compaction.
- **Git-Backed Project Snapshot Synchronization**:
  - Deterministic, cryptographically sealed JSONL project exports and idempotent imports (`memcore/export_project.py`).
  - CLI subcommands `memcore export-project --project <name> [--out .memcore.jsonl]` and `memcore import-project --project <name> [--in .memcore.jsonl]`.
  - Enables daemon-free, port-free cross-machine memory and refusal guard synchronization via Git repositories.

## [2.2.1] - 2026-10-24

### Changed
- **Zero-Config Local Embedding via Shared Global Models (`memcore/embedding.py`)**:
  - Automatically resolves and uses the OS-standard global models directory across platforms:
    - Windows: `%LOCALAPPDATA%\hermes\models`
    - Linux / macOS: `$XDG_DATA_HOME/hermes/models` (falling back to `~/.local/share/hermes/models`)
    - Universal fallback: `~/.hermes/models` (overridable via `HERMES_MODELS_DIR`)
  - Supports in-process ONNX embeddings with `Xenova/multilingual-e5-small` without requiring external services or local network proxies.
  - Automatically shares the model across plugins (`skill-proof`, `memcore`) with zero duplicate disk usage.
- **Removed Private Gateway Coupling (`memcore/embedding.py`, `memcore/__main__.py`)**:
  - Completely purged hardcoded private localhost / custom gateway defaults.
  - Defaults cleanly to in-process local execution, falling back gracefully to lexical FTS5 if no model is present.
  - Standardized public provider presets to `openai`, `openrouter`, and `ollama`.

## [2.2.0] - 2026-10-20

### Added
- **PyPI distribution renamed to `memcore-omnix`** (`pyproject.toml`): the `memcore` name on PyPI is held by an unrelated account (`0.1.0`, uploaded 2025-06-27), so Trusted Publishing could never claim it — the release workflow failed with `invalid-publisher`. `memcore-omnix` is free and the project can now be created on first publish via an OIDC pending publisher.
  - **The import package, the console scripts, and the MCP server name are unchanged** — `import memcore` still works, `memcore` / `memcore-mcp` commands still work, and every client config that registers the `memcore` MCP server (`~/.codex/config.toml`, `~/.claude.json`, `~/.gemini/config/mcp_config.json`) stays valid. Only the *distribution* name on PyPI changed: install with `pip install memcore-omnix`.
- **Release workflow for `skill-proof`** (`.github/workflows/release.yml`): publishes the wheel and sdist to PyPI via Trusted Publishing on release-published, with `skip-existing` so a re-run cannot overwrite an existing version.

## [2.1.0] - 2026-10-18

### Added
- **Automatic Provenance & Zero-Touch Staleness (`mode='strict'`)**:
  - `write._auto_detect_source_file`: ReDoS-immune deterministic extraction of source file paths (e.g. `` `path/to/file.py:12` ``) with strict workspace confinement and 5 MB file size limit.
  - Automatically initializes `verification='source_backed'` when a valid source file is bound or auto-detected.
  - Read-time SHA-256 staleness checking (`core.is_single_source_stale` / `_source_file_is_stale_readonly`): verifies source files on disk at recall time with zero database writes.
  - `mode='strict'` parameter in `core.search()`, `core.recall()`, `MemCore.search()`, and MCP `memory_search`: filters out unverified, disputed, and stale memories, guaranteeing only verified, authoritative current truth is returned.
- **Situational Trigger Indexing (`when_to_use`)**:
  - Added `when_to_use TEXT` column on `memory_version` without bumping schema migrations, preserving `0018_memory_embedding` stability and read-only handle compatibility.
  - Added dedicated Situational Lane (Lane 3.4) in `recall._run_summary_lane` to recall memories matching situational trigger contexts even when the memory body itself has disjoint vocabulary.
  - Parameter `when_to_use` wired through CLI (`--when-to-use`), Python SDK (`mc.remember`), and MCP Server (`memory_remember`, `memory_supersede`).
- **Bitemporal & Delta Recall (`as_of`, `changed_since`, `mode='historical'`)**:
  - Point-in-time time travel (`as_of`): evaluates version validity interval `[valid_from, valid_until)` with tombstone lifetime awareness (prevents future supersede tombstones from blocking historical past versions).
  - Delta synchronization (`changed_since`): retrieves only memories created or modified at or after a timestamp, supporting query-less delta sync.
  - Historical recall (`mode='historical'`): surfaces superseded versions explicitly with `lifecycle='superseded'` and `freshness='historical'`.
- **Layer-3 Progressive Disclosure & Token Budget Packing**:
  - `core.get_memories()` / `MemCore.get()` / MCP `memory_get`: batch hydration of full memory records including version history (`include_history`) and evidence links (`include_evidence`).
  - `core.pack_search_results()`: budget-aware packing governed by `max_tokens`, progressively downgrading overflow rows to Salience L1 previews while guaranteeing top hits fit within the token budget.
- **Skill Graduation Candidate Detection**:
  - `core.skill_graduation_candidates()` in `memcore/doctor.py`: detects frequently-recalled facts and rules (`recall_count >= 5`) as candidates for persistent agent skills.
  - `doctor.stats()` and `doctor.inspect_governance_health()` report graduation candidates using Content-Free metrics to prevent secret leakage in logs.
- **Grouped & Refusal Guard Export**:
  - `--grouped` CLI flag and `export_markdown(grouped=True)`: organizes memories by Type headings (fact, rule, preference, decision) while preserving byte-identical flat output by default.
  - `--include-tombstones` CLI flag: renders active scope-aware refusal guards (tombstones) in an audited section.
- **Comprehensive Upgrade Test Suite (`harness/test_ai_memory_upgrades.py`)**:
  - 8 automated tests covering all 6 architectural capabilities, live staleness detection, read-only access recording bypass, and MCP delta sync.

### Security
- **`<private>` Tag Redaction**:
  - Pre-write scrubbing of `<private>...</private>` tags across all write lanes (`create_memory`, `supersede`), replacing enclosed text with `«redacted:private»` and computing claim fingerprints exclusively from sanitized text.
- **Path Traversal Confinement**:
  - Strictly enforces that auto-detected file paths must reside within the active workspace root (`MEMCORE_WORKSPACE` or `cwd`), preventing arbitrary filesystem binding.

### Fixed
- **Race-Resilient Column Addition (`memcore/store.py`)**:
  - Made `_ensure_column` safe against multi-threaded / multi-process startup races by handling concurrent `sqlite3.OperationalError: duplicate column name`.
- **Read-Only Access Recording in `get_memories` (`memcore/recall.py`)**:
  - Switched write transaction exception handler to `(sqlite3.OperationalError, store.StoreError)` so `get_memories()` with `record_access=True` completes without errors on read-only database connections.
- **Live Staleness Signature Parity (`memcore/client.py`)**:
  - Switched `MemCore.search()` to call `core.is_single_source_stale(sfile, shash)` preventing `AttributeError` on string paths.
- **MCP Delta Search Schema (`memcore/mcp_server.py`)**:
  - Removed mandatory `"query"` requirement in `memory_search` inputSchema, enabling query-less delta queries with `changed_since`.
- **Surface note in the generated MemCore skill body (`memcore/__main__.py`)**:
  - The generated `SKILL.md` for `~/.skill-proof/skills`, `~/.agents/skills`, and `~/.gemini/config/skills` now states which tools each client exposes. The nine names it lists are the **MCP server** tools (Codex CLI, Claude Code, Antigravity / Gemini CLI); in **Hermes**, memcore is the native memory provider (`memory.provider: memcore`) and exposes eight of them natively under the same names — there is no `memory_get` and no `memory_status`.
- **Package version alignment**:
  - `pyproject.toml` and the MCP `SERVER_INFO` handshake now report `2.1.0`, matching the 2.1.0 changelog entry that the earlier commits documented but the version strings did not carry.

## [2.0.0] - 2026-10-18

### Added
- **Universal Stdio MCP Server & One-Command Installer (`memcore/mcp_server.py`, `memcore/mcp_install.py`)**:
  - Exposes 9 governed memory tools (`memory_remember`, `memory_search`, `memory_promote`, `memory_supersede`, `memory_reject`, `memory_feedback`, `memory_review_queue`, `memory_review_decide`, `memory_status`) over stdio JSON-RPC 2.0.
  - `memcore install` / `memcore clients` / `memcore uninstall` auto-registers across Claude Code, Claude Desktop, Gemini CLI, Antigravity, Antigravity IDE, OpenAI Codex CLI, OpenCode, and VS Code (GitHub Copilot).
- **High-Level Python SDK (`memcore/client.py`)**:
  - `MemCore` context-managed client with automatic project/agent/membership provisioning, hybrid FTS5 BM25 + dense vector RRF search, and bitemporal write helpers.
- **Live On-Disk SHA-256 Provenance Verification in `memory_search` (`memcore/mcp_server.py`)**:
  - `_tool_search` joins `memory.current_version_id = memory_version.id` and compares stored `source_hash` against `core.compute_file_hash(source_file)` on disk at recall time, automatically marking modified or deleted source-backed memories as `freshness="stale"`.
- **Zero-Willpower Agent Habit & MCP Instructions (`SERVER_INSTRUCTIONS`)**:
  - Emits `instructions` in the MCP `initialize` response and provisions `~/.gemini/antigravity-cli/mcp/memcore/instructions.md` and `~/.skill-proof/skills/memcore/SKILL.md` during `memcore install`.

### Fixed
- **Vector Recall Noise Floor & Punctuation Guard (`memcore/recall.py`)**:
  - Added `min_sim: float = 0.25` threshold in `_run_vector_lane` and an early `[]` return on punctuation-only queries (`not any(c.isalnum() for c in user_query)`) to prevent low-similarity noise hits when no lexical terms match.
- **Workspace Outer Shim Resolution (`mcp_server.py`)**:
  - Updated root `mcp_server.py` shim to load `memcore/mcp_server.py` via `importlib.util.spec_from_file_location`, preventing circular self-imports when launched from a parent workspace while strictly preserving the Pure PEP 420 Namespace Package invariant (`test_plugin_import_guard.py`).

## [1.0.0] - 2026-10-16

First stable release. Hardening and stabilization: H1 to H9 mechanical fixes, deep-module
refactor (`store.write_tx()` transaction context manager, split search/ingest
monoliths, declarative migration hooks), and recall classification quality raised to
p@3 1.00 on every tier (overall, exact, paraphrase, negation; was 0.86 overall and
0.40 negation). Includes token-efficient preview recall,
write-lane secret scrubbing, C-level vector math, short-circuit search.

### Added
- `memory_search` returns 180-char previews by default (`content_truncated`,
  `content_chars`); full bodies only with `include_content=True`.
- Salience-aware L1 preview scoring (`_truncate_row_content`): prioritizes
  negations, numerical constraints, and file paths when truncating rows to 180 chars.
- In-process C-level pointer vector dot product in `core._vector_search_lane`
  and `core.cosine_similarity` via `array.array('f')` and `operator.mul`,
  reducing p50 query latency from 4.12 ms to 2.42 ms and tail p95 from 8.75 ms to 5.20 ms.
- Longest-first selective substring matching in `core._run_substring_lane`,
  short-circuiting SQLite table scans on spaceless scripts (Thai/CJK) and cutting
  worst-case query latency from 52.80 ms to 12.07 ms.
- `core.scrub_secrets()`: high-precision credential redaction (sk-/ghp_/AKIA/
  xox-/Discord/AWS-secret/PEM) on every write lane — create, supersede,
  reject, semantic candidate/rationale — plus a null-byte guard.
- `BASELINE_{latency,token,resource,error}.md`: baseline documents for this cycle.
- Stabilization hardening regression suite: 27 new tests (23 in
  `harness/test_stabilization_hardening.py` + 2 in `test_secret_scrub.py` +
  2 in `integrations/.../tests/test_plugin.py`), corpus now 568 harness +
  99 integration tests.

### Changed
- Vector cosine similarity is a single implementation in `embedding.cosine_similarity`
  (C-level `array.array('f')` + `operator.mul`, doc norms included); `core.cosine_similarity`
  aliases it and `core._vector_search_lane` uses `sum(map(mul, v_arr, v_arr))` for doc norms.
- Retention ORDER-BY fragments for the default alias are pre-assembled at import
  (`_RETENTION_EXPR_WITH_RECALL_M` / `_RETENTION_EXPR_NO_RECALL_M`) instead of
  %-formatted on every search call.
- `plugin.build_recall_block` dedupes on the row's stored `claim_fingerprint`
  (index 8) before falling back to a content SHA-256.
- `store.pack_vector` / `store.unpack_vector` are direct aliases of the embedding seam.
- `BASELINE_{latency,token,resource,error}.md` refreshed to the post-sweep
  2026-10-16 measurements.
- Phase 3 deep-module refactor: `store.write_tx()` transaction context manager
  replaces the hand-rolled BEGIN/COMMIT/ROLLBACK blocks across write lanes;
  `core.search`/`create_memory` and the ingest/semantic monoliths are split
  behind deep seams; `store._apply_migration` uses declarative hook tables;
  CLI dry-run lanes share the same scan implementation as `--apply`.
- Recall classification quality (owner-approved tuning): phrase-negation
  demotion signal + all-demoted window collapse + fuse-then-demote-then-slice
  ordering — recall p@3 now 1.00 on every tier (was overall 0.86 / negation
  0.40); floors pinned by `test_phase3_demotion_floors`.

### Fixed
- H1: embedding I/O (network/ONNX) no longer runs inside `BEGIN IMMEDIATE`
  write locks — create/supersede precompute the vector before the transaction
  and persist it with SQL only; batched lanes skip inline embedding (backfill
  with `memcore embed`). Removes the slowest writer-lock holder on the
  5-agent store.
- H2: `mark_contradiction` refuses self-pairs and cross-project pairs before
  any write; the `mark-conflict` CLI validates both rows against `--project`
  and prints `error: ...` instead of a traceback; `cmd_embed` embeds only
  candidate/accepted/conflict rows (the old `!= 'tombstoned'` predicate was
  always true and embedded rejected/disabled/superseded rows too).
- H3: lazy `claim_fingerprint` repair survives read-only handles (`mode=ro`) —
  the repair UPDATE is best-effort and the RAM-computed value is still served.
- H4: the per-connection table-columns cache is bounded (<=128 entries), never
  caches empty results, is invalidated when migrations run, and every hit is
  validated against the connection's (database path, schema_version) marker so
  a recycled `id(conn)` or a migrated schema can never serve stale columns.
- H5: the Hermes semantic analyzer falls back to a stdlib schema validator
  when `jsonschema` is unavailable — governance-field injection is still
  refused instead of crashing with ModuleNotFoundError.
- H6: `scrub_secrets` now covers `ingest.append_event` (scrubbed BEFORE the
  payload hash so retries keep the same event identity), `core.deactivate`,
  `core.mark_contradiction` and `ingest.dismiss_unresolved_event`; adds
  `hf_` and `AIza` high-precision patterns.
- H7: circuit-breaker counters and the fastembed model cache are guarded by a
  lock (double-checked locking for the one-time ONNX load).
- H8: export and restore writes are atomic (same-directory temp file +
  `os.replace`) with temp cleanup on failure.
- H9: the Hermes review tools reuse the plugin's cached per-thread connection
  instead of opening a fresh store on every call.
- C1: `decay` and `journal-sweep` dry-run and --apply now share one scan
  implementation, so their lists agree by construction (the decay dry-run
  previously ignored the last_recalled reinforcement window).
- C3: search collapses duplicate claims across the full overfetch window,
  demotes numeric mismatches, and only then slices to `limit` — a compatible
  hit from the window can replace a demoted row instead of being cut early.

### Removed
- `embedding.query_vector_candidates` and its private order dicts — orphaned by the
  C-level vector rewrite; zero references repo-wide (verified by grep + vulture).
- Dead `JOIN memory_version cv` and the `current_content` unpack in
  `core.create_memory`'s idempotency replay; redundant explicit `ROLLBACK`s before
  `raise` (the shared except handler owns rollback); a no-op `_ablation.fake_now()`
  call in `core.search`; duplicated inner imports in `core.py` / `store.py`.
- `.planning/memcore-sweep/` (935 LOC dev tooling) and stale build artifacts
  `dist/`, `memcore.egg-info/`, `.pytest_cache/`.
- `.github/workflows/ci.yml`: the live store is the only verification lane
  (harness 543 green locally, no remote CI dependency).

## [0.8.8] - 2026-10-11

Hardening round & architecture deepening: source-file freshness checks, deep Recall lifecycle, retry-safe journal recording, version sync.

### Added
- **Deep Journal Ingestion & Sweep Module** (`ingest.record` and `ingest.sweep`):
  Consolidates the two-step `append_event` + `process_event` caller
  orchestration into a single `record` call, guaranteeing that ingress
  turns and tool writes are immediately classified without leaked state.
  Adds `sweep` unifying stale built-in mutation dismissals and defer-cap
  resolutions in a single call.
- **Deep Vector Index Module** (`embedding.query_vector_candidates`):
  Encapsulates binary vector deserialization (IEEE 754 float32 little-endian
  BLOBs), cosine similarity computation, and candidate sorting within
  `memcore/embedding.py`. Replaces 60 lines of unpacked math and table joins
  in `core._vector_search_lane` with a single deep seam while preserving
  `store.pack_vector`/`unpack_vector` and `core.cosine_similarity` aliases.
- **Deep Recall Lifecycle Module** (`core.recall`):
  Encapsulates candidate search, RRF rank fusion, access reinforcement
  (`record_recall`), and source-file hash checks into a single interface.
  Callers receive finalized freshness ratings; Hermes prefetch finalizes both
  pinned and query-matched rows through the same path.
- **Source-file staleness checks**: `create_memory`, `supersede`, the Python
  client, and CLI can record a source file. MemCore stores its absolute path
  and SHA-256; `core.check_source_staleness(conn, memory_ids)` marks recalled
  rows stale only when that file's contents change. `source_commit` remains
  provenance context and unrelated Git commits do not flip freshness. Missing
  files, legacy relative paths, or rows without a source hash are skipped safely. Changes leave
  `updated_at` untouched and are audited as `auto_stale_source_changed`.
- **Retry-safe journal recording**: duplicate `ingest.record()` calls resume
  events still pending and return the saved decision and memory link for
  finalized events.
- `core.__version__` single source of truth (namespace-package safe:
  no `memcore/__init__.py`, which would break the plugin import guard).

### Fixed
- **Version drift**: pyproject 0.8.0 / CHANGELOG 0.8.7 / badge 504 all
  synced; badge now reads real suite count (509).
- **Stale `.pytest_cache` verdict**: 2 cached integration fails re-run
  green (10/10 OK) — confirmed stale cache, not a real bug.
- **Self-inflicted import-guard break**: a new `memcore/__init__.py`
  turned the engine into a regular package and broke 3
  `test_plugin_import_guard` tests; reverted to namespace layout and
  moved `__version__` into `core.py`.

### Validation
- Harness 516 OK (2 expected failures, exit 0), incl. 3 new E6 auto-flip
  tests, 4 new deep recall lifecycle tests, and 3 new deep journal
  record/sweep tests.
- doctor clean: integrity ok, journal health=ok (was operator_attention),
  0 pending, 0 unresolved, recovery_ready=True, plugin deploy OK.
- Proof on live store: remember→search round-trip + auto-flip verified.

## [0.8.7] - 2026-10-09

Comprehensive function sweep hardening and performance optimizations. Every change was profiled and validated on an isolated store with regression tests.

### Fixed
- **Backup snapshot same-second collision overwrite** (major).
  `backup_store` formatted timestamps at 1-second resolution (`%Y%m%dT%H%M%SZ`),
  causing consecutive snapshots within the same second to silently overwrite the
  previous file via `os.replace`. It now automatically appends a sequential
  suffix (`_<seq>`) when the target filename exists, and `_SNAPSHOT_RE` regex
  now matches sequential files as managed recovery snapshots.
- **CLI `doctor` falsely failed on isolated/staging databases** (major).
  Running `memcore --db /path/staging.db doctor` failed with exit code 1 because
  it checked the host's global `~/.hermes/config.yaml` and flagged `store_mismatch`.
  Added `--skip-binding-check` flag to `doctor` to skip host profile binding drift
  checks on test/staging stores, and enabled common CLI arguments so `--db` can
  be passed before or after the `doctor` command.
- **Ingest pre-classifier falsely admitted negative statements as memories** (minor).
  `_EXPLICIT_MEMORY_RE` matched `\bremember\b` anywhere in a turn, turning negative
  or conversational phrases (e.g. *"without explicit remember command"*, *"don't remember this"*,
  *"ไม่ต้องจำว่า..."*) into `candidate` memories. Added `_NEGATIVE_MEMORY_RE` to
  intercept negated contexts and route them safely to `review` (`semantic_review_required`).

### Performance
- **`import_memories` batch transaction savepoints** (3.5x throughput gain).
  Replaced per-row `BEGIN IMMEDIATE` + `COMMIT` with per-row `SAVEPOINT item_sp`
  and flushed transactions in 100-item batches. Preserves 100% per-item rollback
  isolation while cutting SQLite disk fsync overhead.
  - n=100: **1,655 rows/s** (was 529 rows/s, 3.1x faster)
  - n=1,000: **906 rows/s** (was 359 rows/s, 2.5x faster)
  - n=4,000: **585 rows/s** (was 165 rows/s, 3.5x faster — 24.2s down to 6.8s)

### Validation
- Harness 506 OK (2 expected failures, exit 0), total run time 50.076s.
- 129 multi-layer functional sweep probes across CLI (59/59), SDK (9/9), Core (22/22),
  Store (5/5), and Subsystems (34/34) all pass 100%.
- New regressions: rapid backup collision preservation (`test_backup_rapid_collision_guard_preserves_all_snapshots`),
  negative ingest context isolation (`test_negative_memory_contexts_prevent_candidate_admission`).

## [0.8.6] - 2026-10-08

Review fixes. Every finding was reproduced on an isolated store before the fix.

### Fixed
- **A refused write no longer destroys an established fact** (blocker).
  `create_memory` marked *both* rows conflict on a contradiction hold, so any
  agent could demote a good fact by writing something wrong — and re-stating the
  truth then contradicted the refused row and was held again, minting a new
  conflict row each attempt. Reproduced: 1 accepted fact + 1 bad write left both
  rows `conflict`, and the next honest write was refused too. Only the refused
  row is demoted now; the established row is untouched and stays recallable.
- **Freshness projection disagreed with `apply_freshness_decay` in both
  directions** (major). It projected `current` -> `stale` in one hop (a 95-day
  row read `stale` while the sweep writes `aging`) and, after the first
  correction, stopped short of `stale` for rows the sweep had already aged. The
  CASE now mirrors the sequential sweep exactly: one step per read, `stale` only
  for a row already stored `aging` and past the threshold, a stored `stale`
  terminal. Verified against a real sweep in both directions.
- **Export could clobber a hand-authored file** (major). `MEMORY.md`,
  `GEMINI.md` and `CLAUDE.md` are files agents *and humans* write; a default
  export silently replaced one. Export now refuses any existing file without the
  `<!-- generated by MemCore` marker and exits 1, with `--force` to override.
  Verified end to end: a hand-written `CLAUDE.md` survives, exit code 1.
- **`--out-dir` ignored itself when `--out` was also given** (major).
- **Export ranked differently from recall** (major) — its query omitted the
  retention term entirely, despite the docstring claiming a single ranking
  definition. It now orders by `core._retention_order`, the same expression
  recall uses.
- **Export skipped the membership gate** (minor): it was the one read path
  without `_membership_role`, so a non-member agent id returned project rows
  that `core.search` refused. Now identical.
- Path errors escaped as tracebacks (`PermissionError` from an unwritable
  target); `OSError`/`ValueError` are reported as `error: ...` and a directory
  target is rejected instead of "succeeding".
- Deleted `AGENT_CONVENTIONS`, a dead dict that disagreed with the
  `HOST_TARGETS` actually in use.

### Rejected after reproduction
- *"A second agent re-writing the same claim is held"* — false. Identical and
  near-identical claims are accepted as corroboration; only a genuine numeric or
  polarity clash is held. The 5-agent corroboration loop (the only route to
  Golden) works.
- *"The ranked-first gate drops Thai memories"* — could not reproduce; the
  substring lane still returned the Thai row.
- *"A stored `aging` row reads back `current` once recalled"* — false, a stored
  non-`current` value wins.

### Validation
- Harness 479 OK (2 expected failures), integration 94 OK, `doctor` exit 0,
  recall baseline p@3 unchanged at 0.81.
- New: the hold cannot poison the truth (2 tests), sweep-matching freshness in
  both directions (1 test), export safety (7 tests: clobber guard, --force,
  idempotence, directory target, membership, rank parity). Each verified to fail
  with its fix reverted.
- 4 host targets: identical sha256.

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
- Harness: 470 tests OK at this release. Export-specific: 19 tests, each verified
  to fail with its fix reverted.
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
