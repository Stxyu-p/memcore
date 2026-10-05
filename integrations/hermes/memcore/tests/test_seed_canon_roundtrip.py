"""Seed canon round-trip acceptance (Task 5).

Proves the spec section 7 acceptance with the Task 1-4 code — no new
production code in this task, test wiring only.

- verbatim trio via the explicit durable path -> exactly one
  auto_corrob_accept audit row, canonical is
  scope=project / lifecycle=accepted / verification=source_backed
- paraphrase trio -> no promotion
- supersede the canonical -> old fingerprint recounts from zero
  (none/vetoed, at_accept == 0)
- recall floor -> baseline p@3 stays >= 0.62

Isolated store per test — NEVER touches ~/.memcore.
"""
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                         '..', '..', '..', '..'))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from memcore import core, ingest, store

SEED_SENTENCE = (
    'MemCore seed canon probe 2026-10-05: '
    'corroboration fires on verbatim repetition.'
)
SEED_USER_TEXT = f'please remember that {SEED_SENTENCE}'

PARAPHRASES = (
    'please remember that MemCore seed paraphrase alpha variant one 2026-10-05.',
    'please remember that MemCore seed paraphrase beta second wording 2026-10-05.',
    'please remember that MemCore seed paraphrase gamma third phrasing 2026-10-05.',
)

# Recall floor fixtures mirror harness/test_recall_baseline.py.
RECALL_FACTS = (
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
RECALL_QUERIES = (
    ('9router gateway port', 'localhost:20128', 'exact'),
    ('AI gateway หลักอยู่ที่ไหน', 'localhost:20128', 'paraphrase'),
    ('9router ใช้พอร์ต 8080', 'localhost:20128', 'negation'),
    ('profile switch command', 'hermes profile use', 'exact'),
    ('เปลี่ยน profile ต้องใช้คำสั่งอะไร', 'hermes profile use', 'paraphrase'),
    ('ใช้ HERMES_PROFILE env เพื่อสลับ profile', 'hermes profile use', 'negation'),
    ('Discord bot token', 'DISCORD_BOT_TOKEN', 'exact'),
    ('โทเคน Discord เก็บไว้ที่ไหน', 'DISCORD_BOT_TOKEN', 'paraphrase'),
    ('fleet agents', 'MIKA', 'exact'),
    ('ทีมมีใครบ้าง', 'MIKA', 'paraphrase'),
    ('คุยกับพี่โชคภาษาอะไร', 'Thai', 'paraphrase'),
    ('converse in English with P Choke', 'Thai', 'negation'),
    ('bot-to-bot language', 'English', 'exact'),
    ('สั่งงาน agent เป็นภาษาไทย', 'English', 'negation'),
    ('lnwjud call method', 'lnwjud_call.py', 'exact'),
    ('เรียก lnwjud ผ่าน MCP ตรง', 'lnwjud_call.py', 'negation'),
    ('scan pacing delay', '250-500ms', 'exact'),
    ('สแกนพักกี่วินาทีต่อหน้า', '250-500ms', 'paraphrase'),
    ('scan page size', '100', 'exact'),
    ('Threads download button', 'Copy Link only', 'paraphrase'),
    ('memory provider หลัก', 'MemCore', 'paraphrase'),
)


def _make_seed_store():
    tmp = tempfile.mkdtemp(prefix='memcore_seed_')
    db = os.path.join(tmp, 'memory.db')
    conn = store.open_store(db)
    conn.execute("INSERT INTO project (id, name) VALUES ('proj-seed','seed')")
    agents = ('agent-alice', 'agent-bob', 'agent-cara')
    for i, aid in enumerate(agents):
        name = aid.removeprefix('agent-')
        conn.execute(
            'INSERT INTO agent (id, name, profile_key) VALUES (?, ?, ?)',
            (aid, name, name),
        )
        conn.execute(
            'INSERT INTO project_membership (project_id, agent_id, role) '
            'VALUES (?, ?, ?)',
            ('proj-seed', aid, 'owner' if i == 0 else 'member'),
        )
    conn.commit()
    return tmp, db, conn, agents


class SeedCanonRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.db, self.conn, self.agents = _make_seed_store()
        self.project = 'proj-seed'

    def tearDown(self):
        try:
            self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except Exception:
            pass
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db + suffix)
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _write_explicit(self, agent, user_text, session):
        event_id, _ = ingest.append_event(
            self.conn, self.project, agent, 'turn',
            session_id=session,
            user_content=user_text,
            assistant_content='Noted.',
        )
        return ingest.process_event(self.conn, event_id)

    def _write_trio(self, user_texts=None):
        texts = user_texts or (SEED_USER_TEXT,) * 3
        results = []
        for agent, text in zip(self.agents, texts):
            res = self._write_explicit(
                agent, text, session=f'seed-{agent.split("-")[1]}')
            results.append(res)
        return results

    def test_verbatim_trio_corrobates(self):
        results = self._write_trio()
        for res in results:
            self.assertEqual(res['decision'], 'private_accepted', res)

        fp = core.fingerprint(SEED_SENTENCE)
        members = core.corroboration_members(self.conn, self.project, fp)
        self.assertEqual(len(members), 3)
        self.assertEqual(len({m[2] for m in members}), 3)

        funnel = store.corroboration_funnel(self.conn)
        self.assertGreaterEqual(funnel['at_accept'], 1)

        canon_id = members[0][0]
        row = self.conn.execute(
            'SELECT scope, lifecycle, verification FROM memory WHERE id=?',
            (canon_id,),
        ).fetchone()
        self.assertEqual(row, ('project', 'accepted', 'source_backed'))

        audit_n = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='auto_corrob_accept'",
        ).fetchone()[0]
        self.assertEqual(audit_n, 1)

        hold_n = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='contradiction-hold'",
        ).fetchone()[0]
        self.assertEqual(hold_n, 0)

    def test_paraphrase_trio_does_not(self):
        results = self._write_trio(user_texts=PARAPHRASES)
        for res in results:
            self.assertEqual(res['decision'], 'private_accepted', res)

        funnel = store.corroboration_funnel(self.conn)
        self.assertEqual(funnel['at_accept'], 0)

        audit_n = self.conn.execute(
            "SELECT COUNT(*) FROM audit_event WHERE action='auto_corrob_accept'",
        ).fetchone()[0]
        self.assertEqual(audit_n, 0)

    def test_supersede_resets_count(self):
        self._write_trio()
        fp_old = core.fingerprint(SEED_SENTENCE)
        members = core.corroboration_members(self.conn, self.project, fp_old)
        canon_id = members[0][0]

        new_content = (
            'MemCore seed canon probe 2026-10-05: '
            'corroboration corrected variant.'
        )
        core.supersede(
            self.conn, canon_id, self.agents[0], new_content,
            reason='seed acceptance correction',
        )

        out = core.maybe_auto_corrob(
            self.conn, self.project, fp_old, self.agents[1])
        self.assertIn(out['action'], ('none', 'vetoed'))
        self.assertLessEqual(out['sources'], 2)

        funnel = store.corroboration_funnel(self.conn)
        self.assertEqual(funnel['at_accept'], 0)

    def test_recall_floor(self):
        for fact in RECALL_FACTS:
            core.create_memory(
                self.conn, self.project, self.agents[0], fact,
                scope='project', lifecycle='accepted',
            )
        hits = total = 0
        for query, expected, tier in RECALL_QUERIES:
            rows = core.search(
                self.conn, self.project, self.agents[0], query, limit=3)
            texts = [r[5] for r in rows]
            if tier == 'negation':
                ok = not texts or expected not in texts[0]
            else:
                ok = any(expected in t for t in texts)
            total += 1
            hits += bool(ok)
        self.assertGreaterEqual(round(hits / total, 2), 0.62)


if __name__ == '__main__':
    unittest.main()
