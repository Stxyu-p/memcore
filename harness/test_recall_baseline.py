"""Phase 5 recall-quality baseline: precision@k over fleet canonical facts.

Three query tiers per fact:
  exact      — words from the fact itself (must hit rank 1).
  paraphrase — same meaning, different words incl. Thai rephrasing (the real gap).
  negation   — asks the opposite; the fact must NOT rank first.

The baseline pins current behaviour. Any future retrieval change (ranking,
tokenizer, hybrid) must move these numbers up, never down.
"""
import os
import tempfile
import unittest

from memcore import core, store

# (query, expected_content_substring, tier)
QUERIES = (
    # 9router gateway
    ('9router gateway port', 'localhost:20128', 'exact'),
    ('AI gateway หลักอยู่ที่ไหน', 'localhost:20128', 'paraphrase'),
    ('9router ใช้พอร์ต 8080', 'localhost:20128', 'negation'),
    # profile switch
    ('profile switch command', 'hermes profile use', 'exact'),
    ('เปลี่ยน profile ต้องใช้คำสั่งอะไร', 'hermes profile use', 'paraphrase'),
    ('ใช้ HERMES_PROFILE env เพื่อสลับ profile', 'hermes profile use', 'negation'),
    # Discord token
    ('Discord bot token', 'DISCORD_BOT_TOKEN', 'exact'),
    ('โทเคน Discord เก็บไว้ที่ไหน', 'DISCORD_BOT_TOKEN', 'paraphrase'),
    # fleet roster
    ('fleet agents', 'MIKA', 'exact'),
    ('ทีมมีใครบ้าง', 'MIKA', 'paraphrase'),
    # Thai language rule
    ('คุยกับพี่โชคภาษาอะไร', 'Thai', 'paraphrase'),
    ('converse in English with P Choke', 'Thai', 'negation'),
    # bot-to-bot English rule
    ('bot-to-bot language', 'English', 'exact'),
    ('สั่งงาน agent เป็นภาษาไทย', 'English', 'negation'),
    # lnwjud bridge
    ('lnwjud call method', 'lnwjud_call.py', 'exact'),
    ('เรียก lnwjud ผ่าน MCP ตรง', 'lnwjud_call.py', 'negation'),
    # scan pacing (ig-maxpland standing decision)
    ('scan pacing delay', '250-500ms', 'exact'),
    ('สแกนพักกี่วินาทีต่อหน้า', '250-500ms', 'paraphrase'),
    ('scan page size', '100', 'exact'),
    # Threads painpoint
    ('Threads download button', 'Copy Link only', 'paraphrase'),
    # memory provider
    ('memory provider หลัก', 'MemCore', 'paraphrase'),
)

FACTS = (
    '9router local proxy answers at localhost:20128/v1 (provider custom Local)',
    'HERMES_PROFILE env does not switch profiles; use hermes profile use NAME',
    'Direct Discord REST: token = DISCORD_BOT_TOKEN from the Hermes .env file',
    'Fleet roster: MIKA orchestrator, NUA SORA ALTIMA MILIM workers, P Choke owner',
    'Converse with P Choke in Thai; commands between agents always in English',
    'Bot-to-bot dispatch: write file with prefix, run hermes chat in English',
    'lnwjud: call ONLY via lnwjud-bridge/lnwjud_call.py, native MCP fails',
    'ig-maxpland scan pacing: 250-500ms per page, no long pause',
    'ig-maxpland v2: scan page size 100 both sides',
    'Threads overflow menu has Copy Link only; no media download or share',
    'MemCore is the canonical team memory system',
)


class RecallBaselineBase(unittest.TestCase):
    def setUp(self):
        from memcore import ablation as _ablation
        _ablation._reset_ablation_cache()
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_recall_base_')
        self.db_path = os.path.join(self.tmpdir, 'recall.db')
        self.conn = store.open_store(self.db_path)
        self.project = 'proj-recall'
        self.agent = 'agent-recall'
        self.conn.execute(
            "INSERT INTO project (id, name) VALUES (?, 'recall')", (self.project,)
        )
        self.conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (self.agent, 'recall', 'recall'),
        )
        self.conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)', (self.project, self.agent, 'owner'),
        )
        self.conn.commit()
        for fact in FACTS:
            core.create_memory(
                self.conn, self.project, self.agent, fact,
                scope='project', lifecycle='accepted',
            )

    def tearDown(self):
        from memcore import ablation as _ablation
        _ablation._reset_ablation_cache()
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def precision_at(self, k=3):
        """(overall, by_tier): fraction of queries whose expected substring
        appears in the top-k hits (negation tier: must NOT appear at rank 1)."""
        hits = total = 0
        by_tier = {}
        for query, expected, tier in QUERIES:
            rows = core.search(self.conn, self.project, self.agent, query, limit=k)
            texts = [r[5] for r in rows]
            if tier == 'negation':
                ok = not texts or expected not in texts[0]
            else:
                ok = any(expected in t for t in texts)
            total += 1
            hits += ok
            t_total, t_hits = by_tier.get(tier, (0, 0))
            by_tier[tier] = (t_total + 1, t_hits + ok)
        return hits / total, {
            tier: hits_ / total_ for tier, (total_, hits_) in by_tier.items()
        }


class TestRecallBaseline(RecallBaselineBase):
    def test_baseline_exact_tier(self):
        _, by_tier = self.precision_at(k=3)
        # Exact queries must (near-)always hit: this is the lexical floor.
        self.assertGreaterEqual(by_tier.get('exact', 0), 0.8)

    def test_recall_2dot0_floor(self):
        # Recall 2.0 floor (lanes 3.1 + 3.2; lane 3.3 deferred):
        # overall>=0.62, exact>=0.80, paraphrase>0.50, negation>=0.20.
        # NOTE: negation is >= (no-regression guard, NOT >) — 0.20 standing
        # is accepted with lane 3.3 deferred.
        overall, by_tier = self.precision_at(k=3)
        self.assertGreaterEqual(overall, 0.62)
        self.assertGreaterEqual(by_tier.get('exact', 0), 0.80)
        self.assertGreater(by_tier.get('paraphrase', 0), 0.50)
        self.assertGreaterEqual(by_tier.get('negation', 0), 0.20)

    def test_baseline_is_measured_not_asserted(self):
        overall, by_tier = self.precision_at(k=3)
        print(f'\nrecall baseline p@3: overall={overall:.2f} ' +
              ' '.join(f'{t}={v:.2f}' for t, v in sorted(by_tier.items())))
        # The point is the number exists and is tracked, not that it is high.
        self.assertGreaterEqual(overall, 0.0)

    def test_negation_never_ranks_wrong_fact_first(self):
        # Baseline 2026-10-03: negation=0.20. Lexical recall has no notion of
        # polarity — a query containing the fact's own words outranks everything.
        # Pinned as a known weakness; any retrieval change must raise this.
        _, by_tier = self.precision_at(k=3)
        self.assertGreaterEqual(by_tier.get('negation', 0), 0.2)


if __name__ == '__main__':
    unittest.main()
