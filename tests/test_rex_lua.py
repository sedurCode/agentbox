"""share/rex/agentbox.lua's Review action, run by a real `lua` against a stand-in for Rex's API (skipped
without lua). It checks the requests the action makes, not Rex itself."""
import shutil
import subprocess
import unittest

from helpers import REAL_RUN, ROOT

HARNESS = ROOT / "tests" / "fixtures" / "rex_review_harness.lua"


@unittest.skipUnless(shutil.which("lua"), "needs lua")
class ReviewActionTests(unittest.TestCase):
    def run_lua(self, scenario):
        r = REAL_RUN(["lua", str(HARNESS), str(ROOT / "share" / "rex"), scenario], capture_output=True, text=True,
                     timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.splitlines()

    def test_picker_lists_clone_profiles_with_counts_and_all(self):
        out = self.run_lua("new")
        self.assertEqual(out[0], "EXEC /ab review --json")
        self.assertIn("ITEM * All profiles 2 ready · 1 not fetched", out)
        self.assertIn("ITEM p p 2 ready · 1 not fetched", out)
        self.assertIn("ITEM q q nothing to review", out)
        self.assertFalse(any("Bad Name" in ln for ln in out))

    def test_opens_a_review_window_in_the_profile_session(self):
        out = self.run_lua("new")
        self.assertIn("CREATE ab:p Review /ab review p nil", out)        # closes when review quits
        self.assertIn("CREATE agentbox Review /ab review nil", self.run_lua("all"))

    def test_existing_session_gets_a_new_window_or_its_review_window_selected(self):
        self.assertIn("NEWWIN S1 Review /ab review p", self.run_lua("existing"))
        out = self.run_lua("existing_window")
        self.assertIn("QUEUE window.select S1 W9", out)
        self.assertFalse(any(ln.startswith(("NEWWIN", "CREATE")) for ln in out))


if __name__ == "__main__":
    unittest.main()
