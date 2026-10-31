import unittest

from helpers import AgentboxTestCase, ab


class RenderLocalKitTests(AgentboxTestCase):
    def render(self, extra="", allow='["api.example.com"]'):
        cfg = self.write_config(f'''
            [tools.t]
            description = "a tool"
            argv = ["{__import__("sys").executable}"]
            [profiles.p]
            workspace = "{self.ws}"
            allow = {allow}
            deny = ["evil.example.com"]
            env = {{ TARGET = "x" }}
            instructions = "Custom note for the agent."
            {extra}
        ''')
        prof = cfg.profile("p")
        kit_dir, digest = ab.render_local_kit(prof, "tok-123")
        return prof, kit_dir, digest, (self.state_dir / "ab-p" / "kit" / "spec.yaml").read_text()

    def test_plain_profile(self):
        _, kit_dir, digest, spec = self.render()
        self.assertTrue(kit_dir.endswith("kit"))
        self.assertEqual(len(digest), 64)
        self.assertIn('"api.example.com"', spec)
        self.assertIn('"evil.example.com"', spec)
        self.assertIn('TARGET: "x"', spec)
        self.assertIn('AGENTBOX_PROFILE: "p"', spec)
        self.assertIn("Custom note for the agent.", spec)
        self.assertIn("Sandbox rules (agentbox)", spec)
        self.assertNotIn("HOSTTOOLS", spec)                  # no tools -> no bridge wiring
        self.assertNotIn("setup:", spec)

    def test_host_tools_add_bridge_port_token_and_client(self):
        prof, _, _, spec = self.render('host_tools = ["t"]\nhost_tools_port = 47123')
        self.assertIn('"localhost:47123"', spec)             # allow-list entry for the bridge
        self.assertIn("http://host.docker.internal:47123", spec)
        self.assertIn('HOSTTOOLS_TOKEN: "tok-123"', spec)
        self.assertIn("hosttool", spec)
        self.assertIn("a tool", spec)                        # tool summary in the instructions
        self.assertTrue((prof.state / "kit" / "files" / "home/.agentbox/hosttool").exists())

    def test_vscode_adds_hosts_without_duplicates(self):
        _, _, _, spec = self.render("vscode = true", allow='["api.example.com", "marketplace.visualstudio.com"]')
        self.assertIn("update.code.visualstudio.com", spec)
        self.assertEqual(spec.count("marketplace.visualstudio.com"), 1)

    def test_digest_changes_with_profile(self):
        _, _, d1, _ = self.render()
        _, _, d2, _ = self.render('agent_args = ["--x"]')
        self.assertNotEqual(d1, d2)


class ShippedKitTests(unittest.TestCase):
    def test_claude_team_env_after_model(self):
        from helpers import ROOT
        spec = (ROOT / "kits" / "claude-team" / "spec.yaml").read_text()
        env = spec.split("environment:\n  variables:\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual(env[0].strip(), 'ANTHROPIC_MODEL: "opusplan"')
        self.assertTrue(env[1].strip().startswith('CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1"'))


if __name__ == "__main__":
    unittest.main()
