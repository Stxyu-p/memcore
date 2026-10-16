"""Gap-2 RED: secret scrubber before disk write.

High-precision patterns only (no env-var names, no normal prose).
Single choke point: core.create_memory + core.supersede.
Isolated tmp stores; NEVER touches live ~/.memcore/memory.db.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, 'C:/Users/BlankScreen/Workspace/memcore')

from memcore import core, ingest, store


def _make_store(tmpdir):
    db_path = os.path.join(tmpdir, 'scrub.db')
    conn = store.open_store(db_path)
    conn.execute("INSERT INTO project (id, name) VALUES ('p', 'p')")
    conn.execute("INSERT INTO agent (id, name, profile_key) VALUES ('a', 'a', 'a')")
    conn.execute("INSERT INTO project_membership (project_id, agent_id, role) "
                 "VALUES ('p', 'a', 'owner')")
    conn.commit()
    return conn, db_path


class TestSecretScrub(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='memcore_scrub_')
        self.conn, self.db_path = _make_store(self.tmpdir)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        for suffix in ('', '-wal', '-shm'):
            try:
                os.unlink(self.db_path + suffix)
            except OSError:
                pass

    def _stored_content(self, mem_id):
        return self.conn.execute(
            'SELECT v.content FROM memory m '
            'JOIN memory_version v ON v.id=m.current_version_id '
            'WHERE m.id=?', (mem_id,)).fetchone()[0]

    def test_openai_key_scrubbed(self):
        mid, _ = core.create_memory(
            self.conn, 'p', 'a',
            'Deploy key sk-abcDEF1234567890abcdef1234567890 is here',
            scope='project', lifecycle='accepted')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn('sk-abcDEF1234567890abcdef1234567890', stored)
        self.assertIn('redacted', stored.lower())

    def test_github_and_aws_keys_scrubbed(self):
        mid, _ = core.create_memory(
            self.conn, 'p', 'a',
            'tokens ghp_abcdefghij1234567890ABCD and AKIAIOSFODNN7EXAMPLE here',
            scope='project', lifecycle='accepted')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn('ghp_abcdefghij1234567890ABCD', stored)
        self.assertNotIn('AKIAIOSFODNN7EXAMPLE', stored)

    def test_private_key_block_scrubbed(self):
        mid, _ = core.create_memory(
            self.conn, 'p', 'a',
            'cert -----BEGIN PRIVATE KEY----- MIIEvwIBADAN here',
            scope='project', lifecycle='accepted')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn('BEGIN PRIVATE KEY', stored)

    def test_aws_secret_key_scrubbed(self):
        mid, _ = core.create_memory(
            self.conn, 'p', 'a',
            'aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY here',
            scope='project', lifecycle='accepted')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn('wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', stored)
        self.assertIn('redacted', stored.lower())

    def test_supersede_scrubs_too(self):
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', 'Service port is 4310',
            scope='project', lifecycle='accepted')
        self.conn.commit()
        core.supersede(
            self.conn, mid, 'a',
            'Rotate to sk-abcDEF1234567890abcdef1234567890 now',
            reason='rotate')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn('sk-abcDEF1234567890abcdef1234567890', stored)

    def test_env_var_names_untouched(self):
        # DISCORD_BOT_TOKEN is a NAME, not a value — must survive.
        body = 'Direct Discord REST: token = DISCORD_BOT_TOKEN from the Hermes .env file'
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', body,
            scope='project', lifecycle='accepted')
        self.conn.commit()
        self.assertEqual(self._stored_content(mid), body)

    def test_normal_prose_untouched(self):
        body = 'The password policy requires rotation every 90 days, no secrets here'
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', body,
            scope='project', lifecycle='accepted')
        self.conn.commit()
        self.assertEqual(self._stored_content(mid), body)

    def test_sadist_near_miss_shapes_survive(self):
        # Near-miss credential LOOKALIKES must NOT be scrubbed: short
        # fragments, bare hex blobs, UUIDs, ports, versions, Thai prose.
        bodies = [
            'sk-short and ghp_short are just fragments, keep them',
            'commit deadbeef1234 merged into main branch today',
            'trace id 550e8400-e29b-41d4-a716-446655440000 for the request',
            'server listens on port 4310 with version v1.2.3',
            'ใช้ token แบบเหรียญรางวัลแลกของ ไม่ใช่รหัสลับอะไร',
            'api key rotation policy is documented in the runbook',
        ]
        for body in bodies:
            mid, _ = core.create_memory(
                self.conn, 'p', 'a', body,
                scope='project', lifecycle='accepted')
            self.conn.commit()
            self.assertEqual(self._stored_content(mid), body, body)

    def test_sadist_empty_and_huge_inputs(self):
        with self.assertRaises(core.MemCoreError):
            core.create_memory(self.conn, 'p', 'a', '   ',
                               scope='project', lifecycle='accepted')
        with self.assertRaises(core.MemCoreError):
            core.create_memory(self.conn, 'p', 'a', None,
                               scope='project', lifecycle='accepted')
        huge = 'deployment note ' * 20000  # ~340KB of clean prose
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', huge,
            scope='project', lifecycle='accepted')
        self.conn.commit()
        self.assertEqual(self._stored_content(mid), huge)

    def test_sadist_unicode_and_null_bytes(self):
        body = 'สถานะ deploy 🚀 ผ่านแล้ว — café naïve Z͑a͉l͍g͗ó — «redacted» literal stays'
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', body,
            scope='project', lifecycle='accepted')
        self.conn.commit()
        self.assertEqual(self._stored_content(mid), body)
        with self.assertRaises(core.MemCoreError):
            core.create_memory(self.conn, 'p', 'a', 'bad\x00null byte',
                               scope='project', lifecycle='accepted')

    def test_sadist_reason_and_journal_fields_scrubbed(self):
        # reason / journal / semantic lanes carry the same scrubber.
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', 'Service port is 4310',
            scope='project', lifecycle='accepted',
            reason='rotate sk-abcDEF1234567890abcdef1234567890 now')
        self.conn.commit()
        ver_reason = self.conn.execute(
            'SELECT reason FROM memory_version WHERE memory_id=? '
            'ORDER BY created_at DESC LIMIT 1', (mid,)).fetchone()[0]
        self.assertNotIn('sk-abcDEF1234567890abcdef1234567890', ver_reason or '')

    def test_sadist_only_secret_content_rejected(self):
        # Content that is ONLY a secret becomes only the marker — still
        # stored (auditable) but carries no credential value.
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', 'sk-abcDEF1234567890abcdef1234567890',
            scope='project', lifecycle='accepted')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn('sk-abcDEF1234567890abcdef1234567890', stored)
        self.assertIn('redacted', stored.lower())


    def test_scrub_huggingface_and_google_api_keys(self):
        """H6: hf_/AIza patterns fire; env names and prose survive."""
        hf_token = 'hf_' + 'a' * 40
        google_key = 'AIza' + 'b' * 35
        body = f'tokens {hf_token} and {google_key} to rotate'
        mid, _ = core.create_memory(
            self.conn, 'p', 'a', body, scope='project', lifecycle='accepted')
        self.conn.commit()
        stored = self._stored_content(mid)
        self.assertNotIn(hf_token, stored)
        self.assertNotIn(google_key, stored)
        self.assertIn('redacted', stored)
        for benign in (
            'HF_TOKEN and GOOGLE_API_KEY are env var names, not values',
            'the huggingface token policy and Google API key rotation runbook',
            'hf_short and AIzaShort are plain words here',
        ):
            mid2, _ = core.create_memory(
                self.conn, 'p', 'a', benign, scope='project',
                lifecycle='candidate')
            self.conn.commit()
            self.assertEqual(self._stored_content(mid2), benign, benign)

    def test_scrub_secrets_on_append_event_and_audit_reasons(self):
        """H6: journal ingress + audit reason lanes never store raw secrets."""
        secret = 'ghp_' + 'c' * 30

        event_id, _ = ingest.append_event(
            self.conn, 'p', 'a', 'turn',
            user_content=f'please rotate {secret} soon')
        stored = self.conn.execute(
            'SELECT user_content FROM ingest_event WHERE id=?',
            (event_id,)).fetchone()[0]
        self.assertNotIn(secret, stored)
        self.assertIn('redacted', stored)

        mid, _ = core.create_memory(
            self.conn, 'p', 'a', 'service alpha rotation note',
            scope='project', lifecycle='candidate')
        self.conn.commit()
        core.deactivate(self.conn, mid, 'a', reason=f'rotate {secret} now')
        detail = self.conn.execute(
            "SELECT detail FROM audit_event WHERE action='deactivate' "
            'ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertNotIn(secret, detail)
        self.assertIn('redacted', detail)

        a, _ = core.create_memory(
            self.conn, 'p', 'a', 'audit scrubbed contradiction claim alpha',
            scope='project', lifecycle='candidate')
        b, _ = core.create_memory(
            self.conn, 'p', 'a', 'audit scrubbed contradiction claim bravo',
            scope='project', lifecycle='candidate')
        self.conn.commit()
        core.mark_contradiction(self.conn, a, b, 'a',
                                reason=f'conflict over {secret}')
        detail = self.conn.execute(
            "SELECT detail FROM audit_event WHERE action='mark_conflict' "
            'ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertNotIn(secret, detail)
        self.assertIn('redacted', detail)

        ev, _ = ingest.append_event(
            self.conn, 'p', 'a', 'turn', user_content='needs operator review')
        self.conn.execute(
            "UPDATE ingest_event SET decision='semantic_review_required' "
            'WHERE id=?', (ev,))
        self.conn.commit()
        ingest.dismiss_unresolved_event(
            self.conn, ev, 'a', rationale=f'dismiss because {secret}')
        detail = self.conn.execute(
            "SELECT detail FROM audit_event WHERE action='journal_dismiss' "
            'ORDER BY id DESC LIMIT 1').fetchone()[0]
        self.assertNotIn(secret, detail)
        self.assertIn('redacted', detail)


if __name__ == '__main__':
    unittest.main()
