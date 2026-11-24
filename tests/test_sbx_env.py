import os
import unittest
from unittest import mock

from helpers import AgentboxTestCase, ab


class SbxEnvTests(AgentboxTestCase):
    """The sandbox must never get the host SSH agent (unless forward_ssh_agent = true)."""

    def cfg(self):
        return self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')

    def test_agent_not_forwarded_on_posix(self):
        with mock.patch.object(ab, "IS_WIN", False), mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent"}):
            self.assertNotIn("SSH_AUTH_SOCK", ab.sbx_env(self.cfg()))

    def test_windows_uses_a_dead_path_not_unset(self):
        # unset makes sbx fall back to the Windows OpenSSH agent named pipe and expose its keys
        with mock.patch.object(ab, "IS_WIN", True), mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "C:/x/agent"}):
            self.assertEqual(ab.sbx_env(self.cfg())["SSH_AUTH_SOCK"], ab.NO_SSH_AGENT_SOCK)
        with mock.patch.object(ab, "IS_WIN", True), mock.patch.dict(os.environ):
            os.environ.pop("SSH_AUTH_SOCK", None)
            self.assertEqual(ab.sbx_env(self.cfg())["SSH_AUTH_SOCK"], ab.NO_SSH_AGENT_SOCK)
        self.assertFalse(os.path.exists(ab.NO_SSH_AGENT_SOCK))
        self.assertEqual(ab.NO_SSH_AGENT_SOCK, "C:\\agentbox-no-ssh-agent")   # absolute, no control bytes

    def test_forward_ssh_agent_true_leaves_environment_alone(self):
        cfg = self.cfg()
        cfg.settings["forward_ssh_agent"] = True
        with mock.patch.dict(os.environ, {"SSH_AUTH_SOCK": "/tmp/agent"}):
            self.assertEqual(ab.sbx_env(cfg)["SSH_AUTH_SOCK"], "/tmp/agent")


if __name__ == "__main__":
    unittest.main()
