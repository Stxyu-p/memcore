# Knowledge Topic Core — Implementation Plan

**Feature:** MemCore Knowledge Topics (multi-domain knowledge organization)
**Status:** Draft for review (พี่โชค)
**Date:** 2026-11-12
**Skills:** brainstorming → writing-plans → executing

## 1. Purpose

ให้ MemCore จัดหมวดหมู่ความรู้เป็น **หัวข้อ (Topics)** ที่เป็น Entity มีโครงสร้าง
— ผู้ใช้สร้าง/แก้/ย้าย/ขยายได้เอง ไม่ใช่ fixed categories — เพื่อรองรับ
"One Core, Multiple Knowledge Domains" (Trading, AI, Business, ...) โดยที่
Topic เป็นแกนร่วมของทุกชั้น: write, read, sleep, vault, MCP, CLI.

## 2. Confirmed Decisions (จากพี่โชค)

| # | เรื่อง | การตัดสินใจ |
|---|---|---|
| 1 | Identifier | Human-readable slug path: `trading/strategies` (ไม่ใช่ uuid) |
| 2 | topic เป็น optional | **ไม่บังคับ** — memory ทำงานปกติโดยไม่มี topic ได้; memory เก่าไม่ต้อง backfill; จะเติม topic ทีหลังเมื่อเข้าข่าย หรือมีกลไกแนะนำให้สร้าง topic |
| 3 | Visibility | Topic catalog = **global** (เห็นรายชื่อทุก topic ทั่วระบบ) แต่ load/recall memory ตาม topic → **กรองตาม project ปัจจุบัน** |
| 4 | Sleep partition | จับคู่ภายใน `project_id + topic_id + memory_type + scope` ก่อน แล้ว Jaccard ภายในกลุ่ม |

## 3. Scope (Phase 1 + 2)

- **Phase 1 — Knowledge Topic Core:** schema, CRUD, hierarchy (parent_id tree), Many-to-Many `memory_topic`, topic filtering ใน write/read, Vault export/import + rebuild.
- **Phase 2 — Memory Intelligence Hardening:** Sleep รู้จัก topic (partition ตามข้อ 4), Topic-specific freshness policy, derived-from lineage (ต่อจากงาน review ก่อนหน้า).

**ไม่อยู่ใน scope:** Trading Knowledge Pack (Phase 3), Trading Integration (Phase 4), JSONL snapshot logic เพิ่มเติม, GitHub metadata fix.

## 4. Data Model

### 4.1 `knowledge_topic` (global catalog)

```sql
CREATE TABLE IF NOT EXISTS knowledge_topic (
    id            TEXT PRIMARY KEY,      -- slug path: 'trading/strategies'
    parent_id     TEXT REFERENCES knowledge_topic(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,          -- display name: 'Trading Strategies'
    description   TEXT,
    freshness_aging_days  INTEGER,        -- Phase 2: topic-specific aging
    freshness_stale_days  INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_knowledge_topic_parent ON knowledge_topic(parent_id);
```

### 4.2 `memory_topic` (Many-to-Many)

```sql
CREATE TABLE IF NOT EXISTS memory_topic (
    memory_id   TEXT NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    topic_id    TEXT NOT NULL REFERENCES knowledge_topic(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (memory_id, topic_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_topic_topic ON memory_topic(topic_id);
```

Key design:
- **Topic catalog global** — `knowledge_topic` ไม่มี project_id เป็น PK
- **Membership per memory** — `memory_topic` ผูกกับ `memory.id` (ซึ่งมี project_id + scope อยู่แล้ว) → ตอน fetch จะ filter ผ่าน join memory → project/scope อัตโนมัติ
- topic รับ `topics=[...]` แบบ optional; `memory_topic` แทรกใน transaction เดียว ถ้าให้ topics มา
- Memory ที่ไม่มี topic → ไม่มี row ใน memory_topic (ทำงานปกติ); มีกลไกแนะนำให้สร้าง topic เมื่อเห็น pattern
- ทุก topic ต้องมี parent chain ถูกต้อง: ถ้าใส่ `trading/strategies` และ `trading` ยังไม่มี → สร้าง parent ให้อัตโนมัติ (auto-create ancestors)
- Cycle guard: ห้ามตั้ง parent เป็น descendant ของตัวเอง

## 5. งานย่อย (Tasks)

### Task 1: Schema + Migration + auto-ancestor helper
- เพิ่มตาราง 2 ตัวใน `schema_defs.py` (migration ใหม่ 0023)
- ฟังก์ชัน `_ensure_topic_chain(conn, topic_slug)` → auto-create parents
- **Tests:** migration สร้างตาราง; `_ensure_topic_chain('trading/strategies')` สร้าง trading + strategies; cycle guard

### Task 2: Write path — create/supersede รับ topics (optional)
- `write.py create_memory(... topics=None)` + `supersede(... topics=None)` — topics เป็น optional
- ถ้า topics ให้มา → validate slug path + auto-create topic chain + insert M2M; ถ้า None → ไม่เติม (ไม่บังคับ)
- validate: topic ต้องเป็น slug path ที่ถูกต้อง (lowercase, alnum + `/` + `-`); auto-create ถ้ายังไม่มี
- `memory_topic` แทรกใน write_tx เดียวกับ memory
- **Tests:** สร้าง memory ระบุ topic → M2M rows; ไม่ระบุ → ไม่มี row M2M; supersede เปลี่ยน topic

### Task 3: Read path — search/list/get รับ topic filter
- `recall.py search(... topics=None)` — กรองผ่าน `JOIN memory_topic` + ยัง filter project/scope เหมือนเดิม
- `list_memories(... topics=None)`; `get_memory_detail` return topics
- **Tests:** search กรอง topic; project isolation (topic เห็นข้าม project แต่ memory ไม่หลุด)

### Task 4: Sleep aware topic
- `sleep.py consolidate_memories` — เปลี่ยน clustering: fetch rows ตาม partition (project+topic+type+scope) แล้ว jaccard ภายใน partition
- **Tests:** memory ต่าง topic ต้องไม่รวมกัน; memory เดียวกัน topic แต่คนละ type ไม่รวม

### Task 5: MCP tools
- เพิ่ม `topic_create`, `topic_list`, `topic_show`, `topic_update`, `topic_delete`
- ขยาย `memory_remember`/`memory_search`/`memory_list` รับ `topics`/`topic`
- **Tests:** MCP registry มี tools; call ผ่าน handler

### Task 6: CLI
- `cli_memory.py` + `__main__.py`: `memcore topic create/list/show/update/delete`, `memcore remember --topic`, `memcore search --topic`
- **Tests:** CLI smoke test

### Task 7: Client SDK
- `client.py`: `topic_create`, `topic_list`, `remember(topics=...)`, `search(topic=...)`
- **Tests:** client integration

### Task 8: Vault export/import
- `export_vault`: เขียน `topics:` ใน frontmatter ของแต่ละ memory (หลัง `type:`)
- `import_vault`: อ่าน `topics:` → ผูก memory_topic หลังสร้าง
- `rebuild_vault_to_db`: topic กลับครบ
- **Tests:** round-trip topic ไม่หาย; rebuild ได้ topic เท่าเดิม

### Task 9: Topic freshness policy (Phase 2)
- เพิ่มคอลัมน์ `freshness_aging_days`/`freshness_stale_days` ใน `knowledge_topic`
- `apply_freshness_decay` ใช้ topic policy override (ถ้ามี) แทน global 30/90
- **Tests:** topic กำหนด aging=7 → memory ใน topic กลายเป็น aging เร็วกว่า

### Task 10: CHANGELOG/README/version + Topic auto-suggest
- กลไกแนะนำ topic: เมื่อสร้าง/จำ memory ที่ไม่มี topic และเข้าข่าย (เช่น ผูกกับ topic ที่มีอยู่แล้ว / ซ้ำ pattern) → เสนอ suggestion (ผ่าน MCP/CLI) ไม่บังคับ
- อัปเดต CHANGELOG, README, `SERVER_INFO` version
- **Tests:** ระบบแนะนำ topic เมื่อ see pattern ที่ตรง

## 6. Global Constraints

- โค้ดลีน (Ponytail): Minimal diff, ไม่เพิ่ม abstraction ที่ไม่ได้ใช้
- Backward compat: `create_memory` ไม่มี topics → ไม่มี M2M เหมือนเดิม; `recall/triples` behavior เดิมไม่พัง
- Security: topic filter ต้องไม่ bypass project/scope isolation — เช่นเดียวกับ privacy fix ก่อนหน้า
- ทุก task ต้องมี test (TDD style แบบ repo: `PYTHONPATH=. python -m unittest harness.test_x`)
- ไม่แก้ CHANGELOG/README อื่นนอกจากที่เกี่ยวข้อง

## 7. Review Focus (คน review ควรตรวจ)

1. **Privacy:** `search(topics=...)` + `list` + MCP — memory private ของคนอื่นใน project ต้องไม่หลุด (เช่นเคย)
2. **Migration idempotent:** `CREATE TABLE IF NOT EXISTS` — ไม่พัง live DB ที่มี migration `0022` ค้าง
3. **Auto-ancestor pitfall:** `_ensure_topic_chain('a/b/c')` ต้องไม่สร้างซ้ำ และ cycle guard กัน `parent=ตัวเอง`
4. **Sleep regression:** partition ใหม่ต้องไม่ทำให้ cluster เดิมหาย/แตก (เทียบก่อน-หลัง)
5. **Vault round-trip:** topic กลับครบทั้ง export→import และ rebuild

## 8. Task 10 — Topic suggestion (implemented 2026-11-12)

Decisions confirmed with พี่โชค:

1. **Trigger:** `remember` ที่ไม่มี topic เท่านั้น (ไม่แตะ search/sleep)
2. **Source:** topic ที่มีอยู่แล้วถ้าใกล้เคียงพอ; เรื่องใหม่ไม่เดา slug เอง — ปล่อยให้ caller ตั้งชื่อ
3. **Surface:** ค่าเริ่มต้นคือแจ้ง (`suggested_topics` ใน MCP response, hint บรรทัดเดียวใน CLI) และมี `topic_mode="auto"` ให้แนบให้เอง

Scoring = **lexical coverage**: สัดส่วน salient token ของ content ใหม่ที่ปรากฏในความจำของ topic นั้น
(`min_coverage=0.3`, `sample=20`, คืนสูงสุด 3) — วัดได้ อธิบายได้ ไม่ต้องมี embedding

Implementation notes:
- ยิง FTS5 ตรง (`memory_version_fts` → `memory_version` → `memory` → `memory_topic`) ด้วย pattern เดียวกับ `_run_fts_lane`
  **ไม่เรียก `recall.search()`** เพราะจะเขียน recall telemetry ทำให้ hit-rate จริงเพี้ยน
- project/scope isolation มาจาก memory join + `_recall_tombstone_guard` เหมือน lane อื่น
- `auto` คำนวณ suggestion **ก่อน** create แล้วส่งเข้า `topics=` (attach ใน transaction เดียว)
- Tests: `harness/test_topic_suggest.py` (13 เคส: ranking, threshold, project/private isolation, no telemetry, MCP 3 modes, CLI smoke)

