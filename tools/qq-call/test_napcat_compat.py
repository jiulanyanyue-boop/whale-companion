import unittest

from napcat_compat import ANCHOR, PATCHED, enable_call_bridge


class PluginCompatibilityTests(unittest.TestCase):
    def test_only_call_plugin_is_added_and_original_gate_remains(self):
        original = (
            "prefix "
            + ANCHOR
            + '\n  "napcat-plugin-cleaner"\n]);\ngetRejectReason(e) { return this.isOfficialPlugin(e); }'
        )
        updated = enable_call_bridge(original)
        self.assertEqual(updated.replace(PATCHED, ANCHOR), original)
        self.assertEqual(enable_call_bridge(updated), updated)

    def test_unrecognized_version_fails_explicitly(self):
        with self.assertRaises(ValueError):
            enable_call_bridge("another plugin loader")


if __name__ == "__main__":
    unittest.main()
