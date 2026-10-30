import unittest

from helpers import AgentboxTestCase, ab


class ConfigTests(AgentboxTestCase):
    def test_defaults_merge_lists_and_override_scalars(self):
        cfg = self.write_config(f'''
            [defaults]
            agent = "codex"
            mode = "clone"
            allow = ["a.example", "shared.example"]
            deny = ["bad.example"]
            kits = ["k1"]
            [profiles.p]
            workspace = "{self.ws}"
            agent = "claude"
            allow = ["shared.example", "b.example"]
            kits = ["k2"]
        ''')
        prof = cfg.profile("p")
        self.assertEqual(prof.d["allow"], ["a.example", "shared.example", "b.example"])  # union, de-duplicated
        self.assertEqual(prof.d["deny"], ["bad.example"])
        self.assertEqual(prof.d["kits"], ["k1", "k2"])
        self.assertEqual(prof.d["agent"], "claude")       # profile overrides defaults
        self.assertEqual(prof.d["mode"], "clone")         # default overrides built-in
        self.assertEqual(prof.sandbox, "ab-p")

    def test_unknown_profile_is_rejected(self):
        cfg = self.write_config("")
        with self.assertRaises(SystemExit):
            cfg.profile("nope")

    def test_workspace_required_and_bad_mode_rejected(self):
        cfg = self.write_config(f'''
            [profiles.nows]
            agent = "shell"
            [profiles.badmode]
            workspace = "{self.ws}"
            mode = "weird"
        ''')
        for n in ("nows", "badmode"):
            with self.assertRaises(SystemExit):
                cfg.profile(n)

    def test_enabled_defaults_true_and_false_is_honoured(self):
        cfg = self.write_config(f'''
            [profiles.on]
            workspace = "{self.ws}"
            [profiles.off]
            workspace = "{self.ws}"
            enabled = false
            [profiles.bad]
            workspace = "{self.ws}"
            enabled = "no"
        ''')
        self.assertTrue(cfg.profile("on").enabled)
        self.assertFalse(cfg.profile("off").enabled)
        with self.assertRaises(SystemExit):
            cfg.profile("bad")

    def test_extra_workspaces_ro_and_rw(self):
        ro, rw, plain = self.tmp / "ro", self.tmp / "rw", self.tmp / "plain"
        cfg = self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
            extra_workspaces = ["{ro}:ro", "{rw}:rw", "{plain}"]
        ''')
        prof = cfg.profile("p")
        self.assertEqual(prof.extras, [(ro, True), (rw, False), (plain, False)])
        self.assertEqual(prof.roots, [self.ws, ro, rw, plain])
        self.assertEqual(prof.writable_roots, [self.ws, rw, plain])

    def test_host_tools_need_a_port(self):
        cfg = self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
            host_tools = ["t"]
        ''')
        with self.assertRaises(SystemExit):
            cfg.profile("p").port

    def test_config_inside_workspace_is_refused_by_the_bridge(self):
        # the config file lives in cfg_dir; make cfg_dir a mounted workspace
        cfg = self.write_config(f'''
            [tools.t]
            argv = ["{__import__("sys").executable}"]
            [profiles.p]
            workspace = "{self.cfg_dir}"
            host_tools = ["t"]
            host_tools_port = 47999
        ''')
        with self.assertRaises(SystemExit):
            ab.Bridge(cfg.profile("p"))


class MainGuardTests(AgentboxTestCase):
    def test_disabled_profile_refused_by_up_code_fresh(self):
        self.write_config(f'''
            [profiles.off]
            workspace = "{self.ws}"
            enabled = false
        ''')
        for cmd in ("up", "code", "fresh"):
            code, _, err = self.run_main(cmd, "off")
            self.assertNotEqual(code, 0, cmd)
            self.assertIn("disabled", err)


if __name__ == "__main__":
    unittest.main()
