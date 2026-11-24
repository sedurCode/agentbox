import unittest
from pathlib import PurePosixPath
from unittest import mock

from helpers import AgentboxTestCase, ab


class VmPathTests(AgentboxTestCase):
    def win(self, p):
        with mock.patch.object(ab, "IS_WIN", True):
            return ab.vm_path(p)

    def test_drive_letter_is_lowercased_and_case_kept(self):
        self.assertEqual(self.win(r"D:\Neural\zenaudiodsp"), "/d/Neural/zenaudiodsp")
        self.assertEqual(self.win(r"e:\git\agentbox"), "/e/git/agentbox")

    def test_forward_slashes_and_mixed(self):
        self.assertEqual(self.win("C:/Users/me/src"), "/c/Users/me/src")
        self.assertEqual(self.win(r"C:\Users/me\src"), "/c/Users/me/src")

    def test_drive_root(self):
        self.assertEqual(self.win("D:\\"), "/d")
        self.assertEqual(self.win("D:"), "/d")

    def test_trailing_separator_and_spaces(self):
        self.assertEqual(self.win("C:\\Program Files\\x\\"), "/c/Program Files/x/")

    def test_unc_and_relative_are_refused(self):
        for bad in ("\\\\server\\share\\x", "relative\\path", "/already/posix"):
            with self.assertRaises(SystemExit):
                self.win(bad)

    def test_posix_host_is_unchanged(self):
        with mock.patch.object(ab, "IS_WIN", False):
            self.assertEqual(ab.vm_path("/Users/me/src"), "/Users/me/src")
            self.assertEqual(ab.vm_path(PurePosixPath("/a/b")), "/a/b")


if __name__ == "__main__":
    unittest.main()
