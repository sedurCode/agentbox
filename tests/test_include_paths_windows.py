"""check_include_path on a Windows host: `\\` is a separator, `X:` is a drive or an NTFS stream, so none may escape the set."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ab


class WindowsIncludePathTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "set"
        (self.root / "skills" / "tdd").mkdir(parents=True)
        (self.root.parent / "outside").mkdir()
        p = mock.patch.object(ab, "IS_WIN", True)
        p.start()
        self.addCleanup(p.stop)

    def check(self, raw):
        return ab.check_include_path(self.root, raw)

    def test_drive_paths_and_streams_are_not_relative(self):
        for raw in ("C:\\Windows", "C:/Windows", "c:x", "skills:stream", "skills/tdd:$DATA", "\\\\server\\share"):
            rel, why = self.check(raw)
            self.assertIsNone(rel, raw)
            self.assertIn("must be relative", why, raw)

    def test_backslash_traversal_is_refused(self):
        for raw in ("..\\outside", "skills\\..\\..\\outside", "skills/..\\..\\outside", "a\\..\\..\\b"):
            rel, why = self.check(raw)
            self.assertIsNone(rel, raw)
            self.assertIn("must not contain", why, raw)

    def test_leading_separators_are_absolute(self):
        for raw in ("\\skills", "/skills"):
            self.assertIn("must be relative", self.check(raw)[1], raw)

    def test_backslash_separated_folder_is_accepted_and_normalised(self):
        self.assertEqual(self.check("skills\\tdd"), ("skills/tdd", ""))
        self.assertEqual(self.check("skills/tdd"), ("skills/tdd", ""))

    @unittest.skipIf(sys.platform == "win32", "cannot create a folder named a:b on NTFS")
    def test_posix_host_still_allows_odd_names(self):
        with mock.patch.object(ab, "IS_WIN", False):
            (self.root / "a:b").mkdir()
            self.assertEqual(self.check("a:b"), ("a:b", ""))


if __name__ == "__main__":
    unittest.main()
