"""Tests for the 6 high-ROI AI-Memory architectural upgrades in MemCore.

Covers:
1. Auto-detected source_file provenance + zero-touch read-time SHA-256 staleness + mode='strict'
2. Situational trigger indexing (when_to_use via memory_version.summary) + summary recall lane
3. Bitemporal & delta recall (as_of, changed_since, mode='historical')
4. Layer-3 progressive disclosure (get_memories / MemCore.get / MCP memory_get + max_tokens packing)
5. <private> tag redaction + grouped & tombstone-aware export
6. Skill graduation candidate detection (content-free in stats/doctor, opt-in content in API)
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest

from memcore import client, core, export as export_mod, mcp_server, store


class AiMemoryUpgradesTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_upgrades_')
        self.db_path = os.path.join(self.tmpdir, 'memory.db')
        self.conn = store.open_store(self.db_path)
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES ('proj-u', 'upgrade-proj')"
        )
        for name, role in (('sora', 'owner'), ('mika', 'member')):
            aid = f'agent-{name}'
            self.conn.execute(
                'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
                (aid, name, name),
            )
            self.conn.execute(
                'INSERT INTO project_membership (project_id, agent_id, role) '
                'VALUES (?, ?, ?)',
                ('proj-u', aid, role),
            )
        self.conn.commit()

    def tearDown(self):
        if getattr(self, 'conn', None) is not None:
            try:
                self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            except Exception:
                pass
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    # ── 1. Auto source_file provenance & strict verification mode ──────────

    def test_auto_source_file_extraction_and_live_staleness(self):
        # Create a real temporary file inside the current working directory's tree
        # or test _auto_detect_source_file with base_dir
        repo_root = pathlib.Path.cwd()
        rel_target = 'memcore/core.py'
        self.assertTrue((repo_root / rel_target).is_file())

        mid, vid = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            f'Core facade in `{rel_target}:19` synchronizes submodule attributes',
            scope='project',
            lifecycle='accepted',
        )
        row = self.conn.execute(
            'SELECT m.verification, v.source_file, v.source_hash '
            'FROM memory m JOIN memory_version v ON v.id = m.current_version_id '
            'WHERE m.id = ?',
            (mid,),
        ).fetchone()
        self.assertEqual(row[0], 'source_backed')
        self.assertEqual(row[1], str((repo_root / rel_target).resolve()))
        self.assertEqual(row[2], core.compute_file_hash(repo_root / rel_target))

        # Outside-workspace traversal must never auto-bind
        outside_file = pathlib.Path(self.tmpdir) / 'outside.py'
        outside_file.write_text('X = 1\n', encoding='utf-8')
        mid_out, _ = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            f'Outside file `{outside_file}` must not auto-bind when outside cwd',
            scope='project',
            lifecycle='accepted',
        )
        out_src = self.conn.execute(
            'SELECT v.source_file FROM memory m '
            'JOIN memory_version v ON v.id = m.current_version_id WHERE m.id = ?',
            (mid_out,),
        ).fetchone()[0]
        self.assertIsNone(out_src)

    def test_strict_mode_filters_stale_and_unverified_memories(self):
        tracked = pathlib.Path(self.tmpdir) / 'service_cfg.py'
        tracked.write_text('TIMEOUT_MS = 5000\n', encoding='utf-8')

        # Memory 1: source-backed by tracked file
        mid_backed, _ = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            'Upstream timeout is 5000 milliseconds',
            scope='project',
            lifecycle='accepted',
            source_file=str(tracked),
        )
        # Memory 2: accepted but unverified
        mid_unver, _ = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            'Upstream retry count is 4 attempts',
            scope='project',
            lifecycle='accepted',
            auto_source_file=False,
        )

        # In standard mode, both can be found
        std_hits = core.search(self.conn, 'proj-u', 'agent-sora', 'Upstream', limit=10)
        std_ids = [r[0] for r in std_hits]
        self.assertIn(mid_backed, std_ids)
        self.assertIn(mid_unver, std_ids)

        # In strict mode, only verified + current (mid_backed) is returned
        strict_hits = core.search(
            self.conn, 'proj-u', 'agent-sora', 'Upstream', limit=10, mode='strict'
        )
        self.assertEqual([r[0] for r in strict_hits], [mid_backed])

        # Now modify the file on disk -> live SHA-256 hash diverges!
        tracked.write_text('TIMEOUT_MS = 9000\n', encoding='utf-8')

        # Standard search projects freshness='stale' at read time without DB writes
        std_after = core.search(self.conn, 'proj-u', 'agent-sora', 'Upstream timeout', limit=5)
        self.assertEqual(std_after[0][0], mid_backed)
        self.assertEqual(std_after[0][4], 'stale')

        # Strict mode drops mid_backed immediately because its source file drifted
        strict_after = core.search(
            self.conn, 'proj-u', 'agent-sora', 'Upstream', limit=10, mode='strict'
        )
        self.assertEqual(strict_after, [])

    # ── 2. Situational Trigger Indexing (when_to_use) ──────────────────────

    def test_when_to_use_trigger_recalls_memory_with_disjoint_content_words(self):
        mid, _ = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            'Configure PRAGMA busy_timeout = 5000 before opening concurrent writers',
            scope='project',
            lifecycle='accepted',
            when_to_use='when troubleshooting database locked errors during parallel worker runs',
            auto_source_file=False,
        )
        # Query uses ONLY words from when_to_use, zero overlap with content
        hits = core.search(
            self.conn,
            'proj-u',
            'agent-mika',
            'troubleshooting locked errors parallel',
            limit=5,
        )
        self.assertTrue(hits)
        self.assertEqual(hits[0][0], mid)
        # Content remains pure (not polluted with when_to_use text)
        self.assertEqual(
            hits[0][5],
            'Configure PRAGMA busy_timeout = 5000 before opening concurrent writers',
        )

        # Supersede preserves when_to_use unless explicitly overridden
        core.supersede(
            self.conn,
            mid,
            'agent-sora',
            'Configure PRAGMA busy_timeout = 8000 before opening concurrent writers',
            reason='raised timeout',
            auto_source_file=False,
        )
        hits_after = core.search(
            self.conn,
            'proj-u',
            'agent-mika',
            'troubleshooting locked errors parallel',
            limit=5,
        )
        self.assertTrue(hits_after)
        self.assertEqual(hits_after[0][0], mid)
        self.assertIn('8000', hits_after[0][5])

    # ── 3. Bitemporal & Delta Recall (as_of, changed_since, historical) ────

    def test_bitemporal_as_of_and_historical_and_changed_since(self):
        mid, v1_id = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            'Analytics warehouse uses port 5432',
            scope='project',
            lifecycle='accepted',
            auto_source_file=False,
        )
        # Pin deterministic timestamps on v1 and v2
        t1 = '2026-09-01T10:00:00Z'
        t_mid = '2026-09-15T12:00:00Z'
        t2 = '2026-10-01T10:00:00Z'
        self.conn.execute(
            'UPDATE memory_version SET valid_from = ?, created_at = ? WHERE id = ?',
            (t1, t1, v1_id),
        )
        v2_id = core.supersede(
            self.conn,
            mid,
            'agent-sora',
            'Analytics warehouse uses port 6432 via PgBouncer',
            reason='switched to pgbouncer',
            auto_source_file=False,
        )
        self.conn.execute(
            'UPDATE memory_version SET valid_until = ? WHERE id = ?',
            (t2, v1_id),
        )
        self.conn.execute(
            'UPDATE memory_version SET valid_from = ?, created_at = ? WHERE id = ?',
            (t2, t2, v2_id),
        )
        self.conn.execute(
            'UPDATE memory SET created_at = ?, updated_at = ? WHERE id = ?',
            (t1, t2, mid),
        )
        self.conn.commit()

        # Default search sees current version (6432)
        live_hits = core.search(self.conn, 'proj-u', 'agent-mika', 'Analytics warehouse port', limit=5)
        self.assertEqual(len(live_hits), 1)
        self.assertIn('6432', live_hits[0][5])

        # Point-in-time search as_of t_mid sees v1 (5432)
        past_hits = core.search(
            self.conn, 'proj-u', 'agent-mika', 'Analytics warehouse port', limit=5, as_of=t_mid
        )
        self.assertEqual(len(past_hits), 1)
        self.assertIn('5432', past_hits[0][5])

        # Historical mode surfaces superseded version when querying for 5432
        hist_hits = core.search(
            self.conn, 'proj-u', 'agent-mika', 'Analytics warehouse 5432', limit=5, mode='historical'
        )
        self.assertTrue(any('5432' in r[5] and r[2] == 'superseded' for r in hist_hits))

        # Delta search with changed_since after t2 returns nothing; at t2 returns mid
        delta_after = core.search(
            self.conn, 'proj-u', 'agent-mika', '', limit=5, changed_since='2026-10-05T00:00:00Z'
        )
        self.assertEqual(delta_after, [])
        delta_hit = core.search(
            self.conn, 'proj-u', 'agent-mika', '', limit=5, changed_since='2026-09-20T00:00:00Z'
        )
        self.assertEqual([r[0] for r in delta_hit], [mid])

    # ── 4. Layer-3 Progressive Disclosure (get_memories & max_tokens) ──────

    def test_get_memories_and_max_tokens_progressive_packing(self):
        with client.MemCore(db_path=self.db_path, project='proj-u', agent='agent-sora') as mc:
            m1 = mc.remember(
                'Primary Redis cluster runs on port 6379 with TLS enabled and strict ACLs across all worker nodes in production.',
                memory_type='fact',
                when_to_use='when configuring Redis connection strings',
                auto_source_file=False,
            )
            m2 = mc.remember(
                'Secondary cache fallback uses local SQLite WAL table when Redis is unreachable for more than 200ms.',
                memory_type='decision',
                auto_source_file=False,
            )
            # Private memory owned by sora
            m_priv = mc.remember(
                'Sora private debug scratchpad note for Redis',
                memory_type='note',
                scope='private',
                auto_source_file=False,
            )

            # Layer-3 batch fetch preserves requested ID order and returns full metadata
            fetched = mc.get([m2, m1], include_history=True)
            self.assertEqual([item['id'] for item in fetched], [m2, m1])
            self.assertEqual(
                fetched[1]['when_to_use'],
                'when configuring Redis connection strings',
            )
            self.assertEqual(len(fetched[1]['history']), 1)
            self.assertGreaterEqual(fetched[1]['recall_count'], 1)

            # Token-budgeted search downgrades overflow items to preview
            packed = mc.search('Redis', limit=5, max_tokens=55)
            self.assertGreaterEqual(len(packed), 1)
            for p in packed:
                self.assertIn('estimated_tokens', p)

        # Scope confinement check: agent-mika must not fetch agent-sora's private memory
        mika_fetched = core.get_memories(
            self.conn, 'proj-u', 'agent-mika', [m1, m_priv]
        )
        self.assertEqual([item['id'] for item in mika_fetched], [m1])

        # MCP tool memory_get integration check
        os.environ['MEMCORE_DB_PATH'] = self.db_path
        try:
            mcp_res = mcp_server._tool_get({
                'memory_ids': [m1],
                'project': 'proj-u',
                'agent': 'agent-mika',
                'include_history': True,
            })
            self.assertTrue(mcp_res['success'])
            self.assertEqual(mcp_res['count'], 1)
            self.assertEqual(mcp_res['results'][0]['id'], m1)
        finally:
            os.environ.pop('MEMCORE_DB_PATH', None)

    # ── 5. <private> Redaction & Grouped / Tombstone Export ────────────────

    def test_private_tag_redaction_and_grouped_tombstone_export(self):
        mid_priv_tag, _ = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            'Connect to internal host <private>db-prod-01.internal.corp</private> on port 5432',
            scope='project',
            memory_type='decision',
            lifecycle='accepted',
            when_to_use='when connecting to production database',
            auto_source_file=False,
        )
        stored_text = self.conn.execute(
            'SELECT v.content FROM memory m '
            'JOIN memory_version v ON v.id = m.current_version_id WHERE m.id = ?',
            (mid_priv_tag,),
        ).fetchone()[0]
        self.assertNotIn('db-prod-01.internal.corp', stored_text)
        self.assertIn('«redacted:private»', stored_text)

        # Create a rejected claim to produce an active project tombstone
        core.reject_value(
            self.conn,
            'proj-u',
            'agent-sora',
            'Never use legacy port 3306',
            reason='MySQL was decommissioned in Q2',
        )

        # Default export remains flat and byte-compatible
        rows = export_mod.rank_for_export(self.conn, 'proj-u', 'agent-sora', 40)
        flat_md = export_mod.render(rows, 'Shared project memory', 'upgrade-proj')
        self.assertNotIn('## Architectural Decisions', flat_md)
        self.assertNotIn('## Do Not Repeat', flat_md)

        # Grouped + tombstone export renders structured sections and refusal guards
        out_file = os.path.join(self.tmpdir, 'AGENTS_GROUPED.md')
        export_mod.export(
            self.conn,
            'proj-u',
            'agent-sora',
            out_path=out_file,
            grouped=True,
            include_tombstones=True,
        )
        grouped_md = pathlib.Path(out_file).read_text(encoding='utf-8')
        self.assertIn('## Architectural Decisions', grouped_md)
        self.assertIn('*(When: when connecting to production database)*', grouped_md)
        self.assertIn('## Do Not Repeat (Active Refusal Guards)', grouped_md)
        self.assertIn('MySQL was decommissioned in Q2', grouped_md)

    # ── 6. Skill Graduation Candidates (Content-Free in Stats/Doctor) ──────

    def test_skill_graduation_candidates_and_content_free_stats(self):
        canary_secret = 'canary skill graduation secret phrase 98765'
        mid, _ = core.create_memory(
            self.conn,
            'proj-u',
            'agent-sora',
            canary_secret,
            scope='project',
            memory_type='decision',
            lifecycle='accepted',
            when_to_use='when running release verification',
            auto_source_file=False,
        )
        self.conn.execute(
            'UPDATE memory SET recall_count = 7 WHERE id = ?', (mid,)
        )
        self.conn.commit()

        # Default skill_graduation_candidates is content-free
        cands_no_content = core.skill_graduation_candidates(self.conn, project_id='proj-u')
        self.assertEqual(len(cands_no_content), 1)
        self.assertEqual(cands_no_content[0]['id'], mid)
        self.assertEqual(cands_no_content[0]['recall_count'], 7)
        self.assertTrue(cands_no_content[0]['has_when_to_use'])
        self.assertNotIn('content', cands_no_content[0])

        # With include_content=True, returns content and when_to_use
        cands_with = core.skill_graduation_candidates(
            self.conn, project_id='proj-u', include_content=True
        )
        self.assertEqual(cands_with[0]['content'], canary_secret)
        self.assertEqual(cands_with[0]['when_to_use'], 'when running release verification')

        # stats() and inspect_governance_health() must NEVER leak canary_secret
        st_json = json.dumps(core.stats(self.conn))
        doc_json = json.dumps(core.inspect_governance_health(self.conn))
        self.assertNotIn(canary_secret, st_json)
        self.assertNotIn(canary_secret, doc_json)
        self.assertIn(mid, st_json)

    def test_client_search_source_file_staleness_and_readonly_get_memories(self):
        # 1. Source-backed memory through client.MemCore
        src_path = (pathlib.Path(self.tmpdir) / 'sdk_target.py').resolve()
        src_path.write_text('VALUE = 100\n', encoding='utf-8')
        with client.MemCore(db_path=self.db_path, project='proj-u', agent='agent-sora') as mc:
            mem = mc.remember(
                'Configuration value is 100',
                source_file=str(src_path),
            )
            # Must not crash with AttributeError
            hits = mc.search('Configuration')
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['id'], mem)
            self.assertEqual(hits[0]['freshness'], 'current')
            self.assertEqual(hits[0]['source_file'], str(src_path))

            # Modify file -> live staleness detected in mc.search()
            src_path.write_text('VALUE = 200\n', encoding='utf-8')
            hits_after = mc.search('Configuration')
            self.assertEqual(hits_after[0]['freshness'], 'stale')

        # 2. get_memories on read-only connection must never crash with AttributeError
        ro_conn = store.open_store_readonly(self.db_path)
        try:
            mems = core.get_memories(ro_conn, 'proj-u', 'agent-sora', [mem], record_access=True)
            self.assertEqual(len(mems), 1)
            self.assertEqual(mems[0]['id'], mem)
        finally:
            ro_conn.close()

        # 3. MCP tool memory_search without query (delta sync via changed_since)
        t_before = '2026-09-01T00:00:00Z'
        mcp_res = mcp_server._tool_search({
            "db_path": self.db_path,
            "changed_since": t_before,
            "project": "proj-u",
            "agent": "agent-sora",
        })
        self.assertTrue(mcp_res["success"])
        self.assertGreaterEqual(mcp_res["count"], 1)


if __name__ == '__main__':
    unittest.main()
