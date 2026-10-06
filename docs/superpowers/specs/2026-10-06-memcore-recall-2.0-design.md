# MemCore Recall 2.0 Design (2026-10-06)

## 1. Goal
Raise recall where it is weak (paraphrase 0.50 / negation 0.20) without
dropping the floor (overall p@3 >= 0.62, exact 1.00). Stdlib-only,
SQLite + WAL + FTS5, daemonless, no new dependency, no UI.

## 2. Current state (FACTs, verified 2026-10-06)
- Baseline (`harness/test_recall_baseline.py`, 3 tests green):
  `overall=0.62 exact=1.00 negation=0.20 paraphrase=0.50`.
- `core.search` (`core.py:1200-1286`): non-ASCII query -> exact
  `instr(v.content, ?)` substring first, then FTS5 `MATCH` with `_fts_query`
  (`core.py:1139-1158`), merged with dedup, rank = pinned >
  lifecycle > verification > freshness > bm25.
- Thai probe: `_tokens('9router gateway หลักอยู่ที่ไหน')` ->
  `['9router','gateway','หลักอยู่ที่ไหน']` — one glued token, so
  `subject_key` mismatches (`gateway หลักอยู่ที่ไหน` vs
  `local proxy answer at`). FTS unicode61 does not segment Thai.
- `contradiction.polarity` exists but `search` never consults it —
  negation queries containing the fact's own words outrank everything.
- Owner pin 2026-10-03: no PyThaiNLP / embeddings / network on any path.

## 3. Design (three minimal lanes, all stdlib)

### 3.1 Thai de-gluing (recall, not precision)
- Problem: Thai glued tokens (`หลักอยู่ที่ไหน`, `ห้ามใช้`) never match
  FTS tokens.
- Fix: in `_fts_query` + `search` exact-fallback, also emit Thai
  character bigrams/trigrams for tokens with `ord>127` and len>=4.
  Example: `หลักอยู่ที่ไหน` -> `หลัก, ลักอ, ...` bigrams match any
  stored row containing the same substring pair. No dict, no dep.
- Ceiling (`ponytail:` O(query_len) extra OR-terms, capped at 32 terms;
  upgrade path is a proper segmenter only if owner approves a dep).

### 3.2 Fleet alias expansion (paraphrase)
- Problem: paraphrase queries use different words (`AI gateway` vs
  `9router`, `โทเคน` vs `token`, `ทีม` vs `fleet roster`).
- Fix: tiny static alias map (~20 entries, fleet canon only) applied to
  the query before `_fts_query`, e.g. `{'ai gateway':'9router',
  'โทเคน':'token discord', 'ทีม':'fleet roster', 'พี่โชค':'thai'}`.
  Query-side only — no stored content changes, no fingerprint churn.
- `ponytail:` static dict, not a framework; add entries only when a
  baseline paraphrase query fails.

### 3.3 Negation-aware demotion (negation)
- Problem: `9router ใช้พอร์ต 8080` ranks the `localhost:20128` fact first.
- Fix: in `search`, if `polarity(query) == -1` (reuse
  `contradiction.polarity`, handles `ไม่/ห้าม/not/...`), demote any hit
  where `is_contradiction_pair(query, content)` is True below the first
  non-contradicting hit (stable, deterministic, no score hacking).
  Negation tier then passes by construction instead of by luck.
- `ponytail:` one `if` in the merge step; no new rank column.

## 4. Explicitly out of scope
- Embeddings / vectors / external segmenter / network ranker.
- Schema change, new table, new CLI surface, dashboard.
- Touching `post_llm_call` candidate-only (ADR-0019) or cron (withdrawn).
- Exact-tier behaviour change: exact queries must still hit rank 1.

## 5. Acceptance gates
1. `harness/test_recall_baseline.py` green with floor enforced:
   overall >= 0.62, exact >= 0.80, paraphrase > 0.50, negation > 0.20.
2. New `harness/test_recall_2dot0.py`: Thai glued query hits,
   alias query hits, negation query demotes contradicting fact.
3. Full gate green: `python -m harness`, integration
   `discover -s tests`, `deploy --check`, `doctor` exit 0.
4. Content-free discipline holds: no memory text in new logs.

## 6. Test plan (TDD, isolated store only)
- Extend baseline with a floor test (`round(hits/total,2) >= 0.62`).
- New file table-driven: (thai-glued -> hit), (alias -> hit),
  (negation -> contradicting fact not rank 1), plus sadist cases:
  empty query, FTS-operator query (`bob's "x" (y)`), 100KB query,
  Thai-only particles query.
- Mutation check: remove alias map / demotion `if` -> at least one new
  test must fail.

## 7. Risks
- Bigram OR-terms flood FTS with false positives -> mitigated by cap 32
  + exact-substring lane still first + baseline floor test.
- Alias map drifts from canon wording -> mitigated by deriving entries
  only from failing baseline queries, reviewed in the spec PR.
- Negation demotion hides the right answer when polarity() false-fires
  (`not` inside a code token) -> mitigated by demote-not-drop (row stays
  in top-k, just not rank 1) + `_NUMBER_RE` boundary rule reused.
