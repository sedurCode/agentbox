import os
import unittest

from helpers import REAL_RUN, AgentboxTestCase, ab, real_sh


class SshAddClassifierTests(unittest.TestCase):
    def test_no_agent_answers(self):
        for rc, out in ((2, "Could not open a connection to your authentication agent.\n"),
                        (2, "Error connecting to agent: No such file or directory"),
                        (2, "error fetching identities: communication with agent failed"),
                        (127, "ssh-add-missing\n"),
                        (127, "sh: 1: ssh-add: not found")):
            self.assertEqual(ab.classify_ssh_add(rc, out), "none", out)

    def test_live_agent(self):
        self.assertEqual(ab.classify_ssh_add(1, "The agent has no identities.\n"), "agent")
        self.assertEqual(ab.classify_ssh_add(0, "256 SHA256:abcdef you@host (ED25519)\n"), "agent")

    def test_listing_wins_over_error_looking_comment(self):
        # a key comment must not be able to make a live agent look absent
        self.assertEqual(ab.classify_ssh_add(0, "256 SHA256:abc Error connecting (ED25519)\n"), "agent")

    def test_unrecognised_is_unknown(self):
        self.assertEqual(ab.classify_ssh_add(5, "something odd"), "unknown")


class TokenClassifierTests(AgentboxTestCase):
    REAL = ["ghp_abc", "gho_abc", "ghu_abc", "ghs_abc", "ghr_abc", "github_pat_abc", "sk-ant-abc", "sk-abc",
            "xoxa-1", "xoxb-1", "xoxp-1", "AKIAABCDEF", "glpat-abc"]
    FAKE = ["", "sentinel", "placeholder-value", "xoxz-1", "akia", "Bearer ghp_x", "proxy-managed"]

    def test_python_classifier(self):
        for v in self.REAL:
            self.assertEqual(ab.classify_token_value(v), "real", v)
        for v in self.FAKE:
            self.assertEqual(ab.classify_token_value(v), "placeholder", v)

    def test_in_vm_probe_agrees_and_never_prints_values(self):
        env = {"PATH": os.environ["PATH"], "HOSTTOOLS_TOKEN": "ghp_skipme", "NOT_RELEVANT": "ghp_nope"}
        expected = {}
        for i, v in enumerate(self.REAL + self.FAKE):
            env[f"X{i}_API_KEY"] = v
            expected[f"X{i}_API_KEY"] = (ab.classify_token_value(v), len(v))
        with real_sh():
            r = REAL_RUN(["sh", "-c", ab.TOKEN_PROBE], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = {n: (c, ln) for n, c, ln in ab.parse_token_vars(r.stdout)}
        self.assertEqual(got, expected)
        for v in self.REAL + ["sentinel", "placeholder-value"]:
            self.assertNotIn(v, r.stdout)
        self.assertNotIn("HOSTTOOLS_TOKEN", r.stdout)
        self.assertNotIn("NOT_RELEVANT", r.stdout)

    def test_parse_drops_malformed_lines(self):
        out = "TOKENVAR GOOD_KEY real 40\nTOKENVAR bad name real 3\nrandom\nTOKENVAR K maybe 3\nTOKENVAR T placeholder x\n"
        self.assertEqual(ab.parse_token_vars(out), [("GOOD_KEY", "real", 40)])


if __name__ == "__main__":
    unittest.main()
