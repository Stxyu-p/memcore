"""The core facade must propagate attribute patches to the module that reads them.

Regression for the module split: `core.X = fake` used to reach a hardcoded list
of four modules, so patching a helper after it moved to a new module silently
stopped working — tests stayed green while asserting nothing.
"""
import unittest

from memcore import core, recall_text, write, write_support


class CoreFacadePatchTests(unittest.TestCase):
    def test_write_helper_patch_reaches_its_real_module(self):
        original = write_support._audit
        marker = lambda *a, **k: None  # noqa: E731
        try:
            core._audit = marker
            self.assertIs(write_support._audit, marker)
            self.assertIs(write._audit, marker)
        finally:
            core._audit = original
        self.assertIs(write_support._audit, original)

    def test_recall_text_constant_patch_reaches_its_real_module(self):
        original = recall_text._FLEET_ALIASES
        try:
            core._FLEET_ALIASES = {}
            self.assertEqual(recall_text._FLEET_ALIASES, {})
        finally:
            core._FLEET_ALIASES = original
        self.assertEqual(recall_text._FLEET_ALIASES, original)


if __name__ == '__main__':
    unittest.main()
