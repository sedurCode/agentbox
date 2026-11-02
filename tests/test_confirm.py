import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import AgentboxTestCase, ab


class ConfirmDialogTests(AgentboxTestCase):
    """Bridge.confirm must allow only on an explicit yes and fail closed otherwise."""

    def bridge(self):
        b = ab.Bridge.__new__(ab.Bridge)
        b.prof = SimpleNamespace(sandbox="ab-t")
        b.log = mock.Mock()
        return b

    def ask(self, platform, tools, rc=0, out="", display=True, side_effect=None, argv=("echo", "x")):
        env = {"DISPLAY": ":0"} if display else {}
        run = mock.Mock(return_value=SimpleNamespace(returncode=rc, stdout=out), side_effect=side_effect)
        with mock.patch.object(ab.sys, "platform", platform), \
             mock.patch.object(ab.shutil, "which", lambda n: f"/usr/bin/{n}" if n in tools else None), \
             mock.patch.dict(os.environ, env, clear=True), \
             mock.patch.object(ab.subprocess, "run", run):
            ok = self.bridge().confirm("t", list(argv), 5)
        return ok, run

    def test_zenity_only_exit_zero_allows(self):
        self.assertTrue(self.ask("linux", ["zenity"])[0])
        for rc in (1, 5, 255):                       # Deny / close, timeout, no display or bad flag
            self.assertFalse(self.ask("linux", ["zenity"], rc=rc)[0], rc)

    def test_zenity_noise_on_stdout_denies(self):
        self.assertFalse(self.ask("linux", ["zenity"], out="warning")[0])

    def test_errors_and_timeouts_deny(self):
        self.assertFalse(self.ask("linux", ["zenity"], side_effect=OSError)[0])
        self.assertFalse(self.ask("linux", ["zenity"], side_effect=subprocess.TimeoutExpired("z", 1))[0])

    def test_no_display_or_no_tool_denies_without_running_anything(self):
        for kw in ({"display": False, "tools": ["zenity"]}, {"tools": []}):
            ok, run = self.ask("linux", **kw)
            self.assertFalse(ok)
            run.assert_not_called()

    def test_zenity_text_is_one_argument_without_markup(self):
        _, run = self.ask("linux", ["zenity"], argv=("echo", "--title=<b>x</b>"))
        cmd = run.call_args[0][0]
        self.assertIn("--no-markup", cmd)
        self.assertEqual(sum("<b>x</b>" in a for a in cmd), 1)

    def test_kdialog_escapes_markup(self):
        ok, run = self.ask("linux", ["kdialog"], argv=("echo", "<b>hide</b><br><img src=x>"))
        self.assertTrue(ok)
        text = run.call_args[0][0][run.call_args[0][0].index("--yesno") + 1]
        self.assertNotIn("<b>", text)
        self.assertNotIn("<img", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertFalse(self.ask("linux", ["kdialog"], rc=1)[0])

    def test_macos_uses_osascript(self):
        ok, run = self.ask("darwin", ["osascript"], out="Allow\n", display=False)
        self.assertTrue(ok)
        self.assertEqual(run.call_args[0][0][0], "osascript")
        self.assertFalse(self.ask("darwin", ["osascript"], out="Deny", display=False)[0])

    def test_host_tool_path_keeps_user_dir_last(self):
        parts = ab.HOST_TOOL_PATH.split(":")
        user = str(ab.HOME / ".local" / "bin")
        if user in parts:
            self.assertEqual(parts[-1], user)
            self.assertLess(parts.index("/usr/bin"), parts.index(user))


if __name__ == "__main__":
    unittest.main()
