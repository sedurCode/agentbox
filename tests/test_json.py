import json
import sys
import unittest

from helpers import AgentboxTestCase, FakeSbx, ab

SECRET = "ghp_THIS_VALUE_MUST_NEVER_APPEAR"
PROBE_OUT = "TOKENVAR GH_TOKEN real 40\nTOKENVAR PROXY_API_KEY placeholder 12\n{\"ok\": true, \"profile\": \"on\"}\n"
NO_AGENT = "Could not open a connection to your authentication agent.\n"


class JsonCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'''
            [tools.flash]
            description = "Flash it"
            argv = ["{sys.executable}", "{{elf}}"]
            params.elf = {{ type = "path", suffixes = [".elf"] }}
            timeout = 30
            confirm = true
            [profiles.on]
            workspace = "{self.ws}"
            host_tools = ["flash"]
            host_tools_port = 47990
            [profiles.off]
            enabled = false
            workspace = "{self.ws}"
            [profiles.plain]
            workspace = "{self.ws}"
        ''')

    def json_of(self, *argv, expect=0):
        code, out, _ = self.run_main(*argv)
        self.assertEqual(code, expect)
        return json.loads(out)               # stdout must be exactly one JSON document


class StatusJsonTests(JsonCase):
    def test_shape(self):
        FakeSbx(exists=True).install(self)
        doc = self.json_of("status", "--json")
        rows = {r["name"]: r for r in doc["profiles"]}
        self.assertEqual(set(rows), {"on", "off", "plain"})
        on = rows["on"]
        self.assertEqual((on["sandbox"], on["enabled"], on["sandbox_exists"]), ("ab-on", True, True))
        self.assertEqual(on["host_tools"], ["flash"])
        self.assertEqual(on["host_tools_port"], 47990)
        self.assertEqual(on["bridge"], {"running": False, "pid": None})
        self.assertFalse(rows["off"]["enabled"])
        self.assertNotIn("sandbox_exists", rows["off"])            # disabled: not probed
        self.assertIsNone(rows["plain"]["host_tools_port"])

    def test_human_output_unchanged(self):
        FakeSbx(exists=False).install(self)
        code, out, _ = self.run_main("status")
        self.assertEqual(code, 0)
        self.assertIn("skipped (disabled)", out)
        self.assertIn("bridge stopped", out)
        with self.assertRaises(ValueError):
            json.loads(out)


class ToolsJsonTests(JsonCase):
    def test_shape(self):
        doc = self.json_of("tools", "on", "--json")
        self.assertEqual(doc["profile"], "on")
        (t,) = doc["tools"]
        self.assertEqual(t["name"], "flash")
        self.assertEqual(t["description"], "Flash it")
        self.assertEqual(t["params"]["elf"]["type"], "path")
        self.assertEqual((t["timeout"], t["confirm"], t["extra_args"]), (30, True, False))
        self.assertNotIn("argv", t)

    def test_no_tools_and_human_output(self):
        self.assertEqual(self.json_of("tools", "plain", "--json"), {"profile": "plain", "tools": []})
        code, out, _ = self.run_main("tools", "plain")
        self.assertEqual((code, out.strip()), (0, "(no host tools)"))
        _, out, _ = self.run_main("tools", "on")
        self.assertIn("hosttool flash elf=<path>", out)

    def test_unknown_tool_is_a_config_error(self):
        (self.cfg_dir / "agentbox.toml").write_text((self.cfg_dir / "agentbox.toml").read_text()
                                                    .replace('host_tools = ["flash"]', 'host_tools = ["ghost"]'))
        code, out, _ = self.run_main("tools", "on", "--json")
        self.assertEqual((code, out), (2, ""))


class DoctorJsonTests(JsonCase):
    def exec_out(self, ssh=(2, NO_AGENT)):
        # the VM would print only classifications; make the fake leak-proof by construction
        return {"TOKENVAR": (0, PROBE_OUT), "ssh-add": ssh}

    def test_all_good_with_warnings(self):
        FakeSbx(exists=True, exec_out=self.exec_out()).install(self)
        code, out, _ = self.run_main("doctor", "on", "--json")
        doc = json.loads(out)
        self.assertEqual(code, 0 if doc["ok"] else 1)
        self.assertTrue(doc["ok"], doc)
        by_status = {}
        for c in doc["checks"]:
            by_status.setdefault(c["status"], []).append(c)
        (warn,) = [c for c in by_status["warn"] if "data" in c]
        self.assertEqual(warn["data"], {"name": "GH_TOKEN", "class": "real", "length": 40})
        self.assertIn("sbx secret ls", warn["hint"])
        self.assertTrue(any(c["data"]["class"] == "placeholder" for c in by_status["info"] if "data" in c))
        self.assertTrue(any(c["message"] == "no SSH agent forwarded" and c["status"] == "pass" for c in doc["checks"]))
        self.assertTrue(all(c["profile"] in (None, "on") for c in doc["checks"]))
        self.assertNotIn(SECRET, out)

    def test_live_ssh_agent_fails_with_exit_1(self):
        FakeSbx(exists=True, exec_out=self.exec_out(ssh=(0, "256 SHA256:abcdef you@host (ED25519)\n"))).install(self)
        code, out, _ = self.run_main("doctor", "on", "--json")
        doc = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(doc["ok"])
        fail = [c for c in doc["checks"] if c["status"] == "fail"]
        self.assertEqual([c["message"] for c in fail], ["no SSH agent forwarded"])
        self.assertIn("sbx daemon", fail[0]["hint"])
        self.assertNotIn("SHA256", out)                            # fingerprints never reach the output

    def test_disabled_and_missing_sandbox_are_skips(self):
        FakeSbx(exists=False).install(self)
        doc = self.json_of("doctor", "--json")
        skips = {(c["profile"], c["message"]) for c in doc["checks"] if c["status"] == "skip"}
        self.assertIn(("off", "skipped (disabled)"), skips)
        self.assertIn(("on", "sandbox not created yet - in-sandbox checks skipped"), skips)
        self.assertTrue(doc["ok"])

    def test_human_output_has_no_json(self):
        FakeSbx(exists=True, exec_out=self.exec_out()).install(self)
        code, out, _ = self.run_main("doctor", "on")
        self.assertEqual(code, 0)
        self.assertIn("  ok   sbx present: sbx 9.9.9", out)
        self.assertIn("  warn GH_TOKEN: looks like a real credential (40 chars)", out)
        self.assertIn("  info PROXY_API_KEY: placeholder/unknown (12 chars)", out)   # gitleaks:allow (a doctor label, not a key)
        self.assertIn("  ok   no SSH agent forwarded", out)
        self.assertTrue(out.rstrip().endswith("all good"))


class ExitCodeTests(JsonCase):
    def test_usage_and_config_errors_are_2(self):
        self.assertEqual(self.run_main("tools", "nope")[0], 2)
        self.assertEqual(self.run_main("up", "off")[0], 2)
        self.assertEqual(self.run_main("status", "--bogus")[0], 2)          # argparse
        (self.cfg_dir / "agentbox.toml").unlink()
        self.assertEqual(self.run_main("status", "--json")[0], 2)

    def test_missing_sandbox_is_3(self):
        FakeSbx(exists=False).install(self)
        self.assertEqual(self.run_main("shell", "plain")[0], 3)
        self.assertEqual(self.run_main("stop", "plain")[0], 3)
        self.assertEqual(self.run_main("audit", "plain")[0], 3)             # nothing was ever created

    def test_constants(self):
        self.assertEqual((ab.EXIT_OK, ab.EXIT_FAIL, ab.EXIT_USAGE, ab.EXIT_NO_SANDBOX), (0, 1, 2, 3))


if __name__ == "__main__":
    unittest.main()
