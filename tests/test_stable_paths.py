"""stable_path: a file inside a Homebrew keg is named through <prefix>/opt/<name>, which outlives an upgrade."""
import os
import unittest

from helpers import AgentboxTestCase, ab


@unittest.skipIf(ab.IS_WIN, "Homebrew layout: symlinks")
class StablePathTests(AgentboxTestCase):
    def brew(self, name="agentbox", version="0.1.0"):
        """<tmp>/prefix with Cellar/<name>/<version>/libexec/bin/agentbox.py, opt/<name> -> the keg, bin/ link."""
        prefix = self.tmp / "prefix"
        keg = prefix / "Cellar" / name / version
        (keg / "libexec" / "bin").mkdir(parents=True)
        (keg / "libexec" / "share" / "rex").mkdir(parents=True)
        script = keg / "libexec" / "bin" / "agentbox.py"
        script.write_text("")
        (prefix / "opt").mkdir(exist_ok=True)
        opt = prefix / "opt" / name
        if opt.is_symlink():
            opt.unlink()
        opt.symlink_to(os.path.relpath(keg, opt.parent))
        (prefix / "bin").mkdir(exist_ok=True)
        return prefix, keg, script

    def test_keg_path_becomes_the_opt_path(self):
        prefix, keg, script = self.brew()
        self.assertEqual(ab.stable_path(script), prefix / "opt" / "agentbox" / "libexec" / "bin" / "agentbox.py")
        via_opt = prefix / "opt" / "agentbox" / "libexec" / "bin" / "agentbox.py"
        self.assertEqual(ab.stable_path(via_opt), via_opt)          # however it was started

    def test_opt_path_survives_an_upgrade(self):
        prefix, _, script = self.brew()
        stable = ab.stable_path(script)
        self.brew(version="0.1.1")                                  # brew upgrade: opt now points at the new keg
        (prefix / "Cellar" / "agentbox" / "0.1.0" / "libexec" / "bin" / "agentbox.py").unlink()   # brew cleanup
        self.assertTrue(stable.exists())

    def test_opt_pointing_elsewhere_keeps_the_real_path(self):
        prefix, _, script = self.brew()
        self.brew(version="0.1.1")                                  # an old keg, not the linked one
        self.assertEqual(ab.stable_path(script), script.resolve())

    def test_non_brew_paths_are_just_resolved(self):
        f = self.tmp / "x" / "agentbox.py"
        f.parent.mkdir()
        f.write_text("")
        link = self.tmp / "link.py"
        link.symlink_to(f)
        self.assertEqual(ab.stable_path(link), f.resolve())
        self.assertEqual(ab.stable_path(ab.SCRIPT), ab.SCRIPT)      # the test checkout is no keg

    def test_rex_block_uses_the_opt_path(self):
        prefix, keg, _ = self.brew()
        from unittest import mock
        share = ab.stable_path(keg / "libexec" / "bin" / "agentbox.py").parent.parent / "share"
        with mock.patch.object(ab, "SHARE_DIR", share):
            block = ab.rex_block()
        self.assertIn(str(prefix / "opt" / "agentbox" / "libexec" / "share" / "rex"), block)
        self.assertNotIn("Cellar", block)
