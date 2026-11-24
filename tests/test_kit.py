import contextlib
import io
import unittest

from helpers import ROOT, AgentboxTestCase, ab

LIFECYCLE = "com.docker.sandbox/lifecycle@1"
NETWORK = "com.docker.sandbox/network-policy@1"
CONTEXT = "com.docker.sandbox/agent-context@2"


def cap(spec, kind):
    found = [c for c in spec["capabilities"] if c["type"] == kind]
    return found[0]["config"] if found else None


class RenderLocalKitTests(AgentboxTestCase):
    def config(self, extra="", allow='["api.example.com"]'):
        return self.write_config(f'''
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

    def render(self, extra="", allow='["api.example.com"]'):
        prof = self.config(extra, allow).profile("p")
        kit_dir, digest = ab.render_local_kit(prof, "tok-123")
        text = ab.kit_descriptor(ab.Path(kit_dir)).read_text()
        spec, _ = ab.local_kit_spec(prof, "tok-123")
        return prof, kit_dir, digest, text, spec

    def test_plain_profile(self):
        _, kit_dir, digest, text, spec = self.render()
        self.assertEqual(ab.Path(kit_dir).name, ab.LOCAL_KIT_NAME)   # the kit's name inside the sandbox
        self.assertEqual(len(digest), 64)
        self.assertTrue(text.startswith(ab.KIT_SYNTAX + "\n"))       # selects the v3 frontend
        self.assertEqual((spec["schemaVersion"], spec["kind"]), ("3", "mixin"))
        self.assertNotIn("name", spec)                               # v3: identity is the directory
        self.assertEqual(cap(spec, NETWORK), {"runtime": {"allow": ["api.example.com"], "deny": ["evil.example.com"]}})
        self.assertEqual(spec["args"]["TARGET"], {"default": "x", "env": "TARGET"})
        self.assertEqual(spec["args"]["AGENTBOX_PROFILE"], {"default": "p", "env": "AGENTBOX_PROFILE"})
        body = cap(spec, CONTEXT)["content"]
        self.assertIn("Custom note for the agent.", body)
        self.assertIn("Sandbox rules (agentbox)", body)
        self.assertNotIn("HOSTTOOLS", text)                          # no tools -> no bridge wiring
        self.assertIsNone(cap(spec, LIFECYCLE))
        self.assertEqual(sorted(p.name for p in ab.Path(kit_dir).iterdir()), [f"{ab.LOCAL_KIT_NAME}.yaml"])

    def test_host_tools_add_bridge_port_token_and_client(self):
        _, _, _, text, spec = self.render('host_tools = ["t"]\nhost_tools_port = 47123')
        self.assertIn("localhost:47123", cap(spec, NETWORK)["runtime"]["allow"])   # the bridge
        self.assertEqual(spec["args"]["HOSTTOOLS_URL"]["default"], "http://host.docker.internal:47123")
        self.assertEqual(spec["args"]["HOSTTOOLS_TOKEN"], {"default": "tok-123", "env": "HOSTTOOLS_TOKEN"})
        self.assertIn("a tool", cap(spec, CONTEXT)["content"])         # tool summary in the instructions
        life = cap(spec, LIFECYCLE)
        client = f"{ab.VM_HOME}/.agentbox/hosttool"
        self.assertEqual([f["path"] for f in life["files"]], [client])
        self.assertEqual(life["files"][0]["content"], (ab.SHARE_DIR / "hosttool").read_text())
        self.assertEqual(life["startup"][0]["user"], "0")            # files land after install hooks
        self.assertIn(client, life["startup"][0]["command"])
        self.assertNotIn("install", life)
        self.assertIn('"tok-123"', text)

    def test_vscode_adds_hosts_without_duplicates(self):
        _, _, _, text, spec = self.render("vscode = true", allow='["api.example.com", "marketplace.visualstudio.com"]')
        self.assertIn("update.code.visualstudio.com", cap(spec, NETWORK)["runtime"]["allow"])
        self.assertEqual(text.count("marketplace.visualstudio.com"), 1)

    def test_digest_changes_with_profile(self):
        d1 = self.render()[2]
        d2 = self.render('agent_args = ["--x"]')[2]
        self.assertNotEqual(d1, d2)

    def test_old_v2_kit_dir_is_replaced(self):
        prof = self.config().profile("p")
        old = prof.state / "kit"
        (old / "files" / "home").mkdir(parents=True)
        (old / "spec.yaml").write_text('schemaVersion: "2"\n')
        kit_dir, _ = ab.render_local_kit(prof, "tok")
        self.assertFalse((old / "spec.yaml").exists())
        self.assertFalse((old / "files").exists())
        self.assertTrue(ab.is_kit_dir(ab.Path(kit_dir)))

    def test_kit_placeholders_in_profile_values_are_refused(self):
        for extra in ('instructions = "use ${{ kit.args.x }}"', 'env = { A = "${{ kit.env.HOME }}" }'):
            prof = self.write_config(f'''
                [profiles.p]
                workspace = "{self.ws}"
                {extra}
            ''').profile("p")
            with self.assertRaises(SystemExit) as cm:
                ab.render_local_kit(prof, "tok")
            self.assertEqual(cm.exception.code, ab.EXIT_USAGE)

    def test_bad_env_names_are_refused(self):
        prof = self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
            env = {{ "A-B" = "1" }}
        ''').profile("p")
        with self.assertRaises(SystemExit):
            ab.render_local_kit(prof, "tok")


class KitResolutionTests(AgentboxTestCase):
    def test_v3_kit_by_name(self):
        d = self.cfg_dir / "kits" / "mine"
        d.mkdir(parents=True)
        (d / "mine.yaml").write_text(ab.KIT_SYNTAX + "\n")
        self.assertEqual(ab.resolve_kit("mine"), d)
        self.assertTrue(ab.kit_installed("mine"))

    def test_v2_kit_is_refused_with_a_migration_hint(self):
        d = self.cfg_dir / "kits" / "old"
        d.mkdir(parents=True)
        (d / "spec.yaml").write_text('schemaVersion: "2"\n')
        self.assertFalse(ab.kit_installed("old"))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            ab.resolve_kit("old")
        self.assertIn("v2 kit grammar", err.getvalue())

    def test_a_spec_yaml_beside_a_v3_descriptor_still_counts_as_v2(self):
        d = self.cfg_dir / "kits" / "both"
        d.mkdir(parents=True)
        (d / "both.yaml").write_text(ab.KIT_SYNTAX + "\n")
        (d / "spec.yaml").write_text('schemaVersion: "2"\n')      # sbx reads this one first
        self.assertFalse(ab.kit_installed("both"))
        err = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(err):
            ab.resolve_kit("both")
        self.assertIn("move spec.yaml out", err.getvalue())

    def test_missing_kit(self):
        with self.assertRaises(SystemExit):
            ab.resolve_kit("nope")


class ShippedKitTests(unittest.TestCase):
    KITS = ("claude-team", "embedded-arm", "junie", "python-node")

    def descriptor(self, name):
        return (ROOT / "kits" / name / f"{name}.yaml").read_text()

    def test_every_kit_is_v3(self):
        self.assertEqual(sorted(p.name for p in (ROOT / "kits").iterdir() if p.is_dir()), list(self.KITS))
        for k in self.KITS:
            text = self.descriptor(k)
            self.assertTrue(text.startswith(ab.KIT_SYNTAX + "\n"), k)
            self.assertIn('\nschemaVersion: "3"\n', text, k)
            self.assertNotIn("\nname:", text, k)                     # strict decode rejects the v2 field
            self.assertFalse((ROOT / "kits" / k / "spec.yaml").exists(), k)
            # `licenses:` makes sbx build even a declaration-only mixin (needs Docker on the host)
            self.assertNotIn("\nlicenses:", text, k)

    def test_only_the_workload_has_a_recipe(self):
        for k in self.KITS:
            recipe = ROOT / "kits" / k / f"{k}.dockerfile"
            self.assertEqual(recipe.exists(), k == "junie", k)
        self.assertIn("\nkind: workload\n", self.descriptor("junie"))
        self.assertIn("com.docker.sandbox/sbx@1", self.descriptor("junie"))

    def test_hooks_that_go_through_the_proxy_declare_it(self):
        for k in ("python-node", "embedded-arm", "junie"):
            self.assertIn("env: [HTTP_PROXY, HTTPS_PROXY", self.descriptor(k), k)

    def test_claude_team_env_after_model(self):
        text = self.descriptor("claude-team")
        args = text.split("\nargs:\n")[1].split("\n\n")[0]
        self.assertLess(args.index("ANTHROPIC_MODEL:"), args.index("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:"))
        self.assertIn('default: "opus"', args)
        self.assertIn("env: ANTHROPIC_MODEL", args)


if __name__ == "__main__":
    unittest.main()
