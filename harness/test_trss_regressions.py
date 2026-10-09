import unittest

from memcore import ingest


class AdmissionRegressionTests(unittest.TestCase):
    def test_thai_glued_durable_signals(self):
        for text in ('ต่อไปนี้ใช้ B.AI', 'เราตกลงกันว่าจะใช้ Python',
                     'ฉันชอบธีมมืด', 'ช่วยจำไว้ว่าพี่ชอบชา',
                     'เราตัดสินใจกันแล้วว่าจะใช้ SQLite'):
            with self.subTest(text=text):
                self.assertEqual(ingest.classify_user_text(text)[0], 'candidate')

    def test_standalone_english_test_is_trivial(self):
        self.assertEqual(ingest.classify_user_text('test'), ('ignore', 'trivial', ''))
        self.assertEqual(ingest.classify_user_text('test the deployment')[0], 'review')

    def test_standalone_greetings_do_not_queue_semantic_calls(self):
        for text in ('สวัสดีค่ะ', 'ขอบคุณนะคะ', 'ทดสอบ', 'เทส'):
            with self.subTest(text=text):
                self.assertEqual(ingest.classify_user_text(text)[0], 'ignore')
        self.assertEqual(ingest.classify_user_text('ขอบคุณ ต่อไปนี้ใช้ Python')[0], 'candidate')

    def test_oversized_request_is_reviewed_not_truncated_into_memory(self):
        self.assertEqual(ingest.classify_user_text('remember that ' + 'x' * 4100),
                         ('review', 'semantic_review_required', ''))

    def test_negative_memory_contexts_prevent_candidate_admission(self):
        negatives = (
            'discussion without explicit remember command',
            'please don\'t remember this',
            'do not remember that secret',
            'never remember this password',
            'ไม่ต้องจำเรื่องนี้',
            'อย่าจำว่าเราชอบชา',
            'ห้ามจำคีย์นี้',
        )
        for text in negatives:
            with self.subTest(text=text):
                status, decision, _ = ingest.classify_user_text(text)
                self.assertNotEqual(status, 'candidate')
                self.assertEqual(decision, 'semantic_review_required')

