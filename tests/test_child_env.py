import os
import unittest
from unittest import mock

from helpers import AgentboxTestCase, ab

SECRETS = {"GH_TOKEN": "x", "ANTHROPIC_API_KEY": "y", "SSH_AUTH_SOCK": "/s", "AWS_SECRET_ACCESS_KEY": "z"}


class ChildEnvTests(AgentboxTestCase):
    def env(self, is_win, extra):
        with mock.patch.object(ab, "IS_WIN", is_win), mock.patch.dict(os.environ, {**SECRETS, **extra}, clear=True):
            return ab.child_env()

    def test_posix_keeps_only_the_allow_list(self):
        got = self.env(False, {"PATH": "/bin", "HOME": "/h", "LANG": "C", "USERPROFILE": "ignored"})
        self.assertEqual(got, {"PATH": "/bin", "HOME": "/h", "LANG": "C"})

    def test_windows_adds_what_python_and_sbx_need(self):
        got = self.env(True, {"Path": "C:/x", "USERPROFILE": "C:/Users/me", "SYSTEMROOT": "C:/Windows",
                              "LocalAppData": "C:/L", "TEMP": "C:/T"})
        self.assertEqual({k.upper() for k in got}, {"PATH", "USERPROFILE", "SYSTEMROOT", "LOCALAPPDATA", "TEMP"})

    def test_secrets_never_pass_on_either_platform(self):
        for is_win in (False, True):
            got = self.env(is_win, {"PATH": "p"})
            for k in SECRETS:
                self.assertNotIn(k, got)


if __name__ == "__main__":
    unittest.main()
