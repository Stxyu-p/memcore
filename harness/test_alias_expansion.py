"""Bilingual alias expansion: cross-language bridges without false positives."""
import unittest

from memcore.recall_text import _expand_aliases


class AliasExpansionTests(unittest.TestCase):
    def test_english_query_reaches_thai_fact(self):
        self.assertIn('thinkpad', _expand_aliases('laptop specs'))
        self.assertIn('แบต', _expand_aliases('battery health'))

    def test_thai_query_reaches_english_fact(self):
        self.assertIn('driver', _expand_aliases('ไดรเวอร์'))
        self.assertIn('ram', _expand_aliases('หน่วยความจำ'))

    def test_ascii_alias_matches_whole_words_only(self):
        # 'ram' must not fire inside 'program'; 'cpu' must not fire inside 'cpufreq'.
        self.assertNotIn('หน่วยความจำ', _expand_aliases('program'))
        self.assertNotIn('โปรเซสเซอร์', _expand_aliases('cpufreq'))

    def test_thai_alias_matches_glued_substring(self):
        self.assertIn('battery', _expand_aliases('แบตเหลือเท่าไหร่'))


if __name__ == '__main__':
    unittest.main()
