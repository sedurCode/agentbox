"""main() appends the package managers' bin dirs to PATH, so sbx is found when a GUI app starts agentbox."""
import os
from unittest import mock

from helpers import AgentboxTestCase, ab


class PackagePathTests(AgentboxTestCase):
    def test_appends_missing_dirs_after_the_callers(self):
        got = ab.with_package_dirs("/usr/bin:/bin", isdir=lambda d: d == "/opt/homebrew/bin")
        self.assertEqual(got, os.pathsep.join(["/usr/bin", "/bin", "/opt/homebrew/bin"]))

    def test_keeps_the_callers_order_and_adds_nothing_twice(self):
        path = os.pathsep.join(["/opt/homebrew/bin", "/mine"])
        self.assertEqual(ab.with_package_dirs(path, isdir=lambda d: True),
                         os.pathsep.join(["/opt/homebrew/bin", "/mine", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin"]))

    def test_skips_dirs_that_dont_exist(self):
        self.assertEqual(ab.with_package_dirs("/usr/bin", isdir=lambda d: False), "/usr/bin")

    def test_main_applies_it(self):
        if ab.IS_WIN:
            self.skipTest("POSIX only")
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}), \
                mock.patch.object(ab, "PACKAGE_BIN_DIRS", (str(self.tmp),)):
            self.run_main("--version")              # exits before dispatch: argparse handles it
            self.run_main("setup", "--json")
            self.assertTrue(os.environ["PATH"].endswith(os.pathsep + str(self.tmp)))
