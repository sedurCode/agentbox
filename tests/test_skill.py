import json
import os
from unittest import mock

from helpers import ROOT, AgentboxTestCase, ab


class SkillTests(AgentboxTestCase):
    """`agentbox skill`: a temp HOME, no config file, and no sbx (any call fails the test)."""

    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CONFIG_DIR", "CODEX_HOME")}
        # Path.home() reads USERPROFILE on Windows, HOME elsewhere: set both, or the real home is written
        p = mock.patch.dict(os.environ, env | {"HOME": str(self.home), "USERPROFILE": str(self.home)}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        self.claude = self.home / ".claude/skills/agentbox/SKILL.md"
        self.agents = self.home / ".agents/skills/agentbox/SKILL.md"

    def states(self):
        code, out, _ = self.run_main("skill", "status", "--json")
        self.assertEqual(code, 0)
        return {r["agent"]: r["state"] for r in json.loads(out)["locations"]}

    def test_stub_has_front_matter_marker_and_points_at_show(self):
        stub = ab.SKILL_STUB
        self.assertTrue(stub.startswith("---\nname: agentbox\ndescription: "))
        front = stub.split("---\n")[1]
        for trigger in ("agentbox", "sandbox", "sbx", "run this in a box"):
            self.assertIn(trigger, front)
        body = stub.split("---\n", 2)[2]
        self.assertIn(ab.SKILL_MARKER, body)
        self.assertIn("agentbox skill show", body)

    def test_show_prints_the_shipped_guide(self):
        code, out, _ = self.run_main("skill", "show")
        self.assertEqual(code, 0)
        self.assertEqual(out, (ROOT / "share/skill/guide.md").read_text())
        self.assertIn("Ground rules", out)

    def test_needs_no_config(self):
        self.assertFalse((self.cfg_dir / "agentbox.toml").exists())
        self.assertEqual(self.states(), {"claude": "missing", "agents": "missing"})

    def test_setup_then_status_then_remove(self):
        code, out, _ = self.run_main("skill", "setup")
        self.assertEqual(code, 0)
        self.assertIn("installed", out)
        self.assertEqual(self.claude.read_text(), ab.SKILL_STUB)
        self.assertEqual(self.agents.read_text(), ab.SKILL_STUB)
        self.assertEqual(self.states(), {"claude": "current", "agents": "current"})
        code, out, _ = self.run_main("skill", "setup")
        self.assertIn("claude: current", out)
        code, out, _ = self.run_main("skill", "remove")
        self.assertEqual(code, 0)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.claude.parent.exists())
        self.assertEqual(self.states(), {"claude": "missing", "agents": "missing"})

    def test_outdated_stub_is_rewritten(self):
        self.agents.parent.mkdir(parents=True)
        self.agents.write_text(f"old stub\n{ab.SKILL_MARKER} v0 -->\n")
        self.assertEqual(self.states()["agents"], "outdated")
        code, out, _ = self.run_main("skill", "setup")
        self.assertIn("agents: updated", out)
        self.assertEqual(self.agents.read_text(), ab.SKILL_STUB)

    def test_foreign_files_are_left_alone(self):
        mine = "---\nname: agentbox\n---\nmy own notes\n"
        self.claude.parent.mkdir(parents=True)
        self.claude.write_text(mine)
        self.assertEqual(self.states()["claude"], "foreign")
        code, _, err = self.run_main("skill", "setup")
        self.assertEqual(code, 0)
        self.assertIn("foreign", err)
        self.assertEqual(self.claude.read_text(), mine)
        self.assertEqual(self.agents.read_text(), ab.SKILL_STUB)
        self.run_main("skill", "remove")
        self.assertEqual(self.claude.read_text(), mine)
        self.assertFalse(self.agents.exists())

    def test_remove_keeps_other_files_in_the_folder(self):
        self.run_main("skill", "setup")
        (self.agents.parent / "notes.md").write_text("mine")
        self.run_main("skill", "remove")
        self.assertFalse(self.agents.exists())
        self.assertTrue((self.agents.parent / "notes.md").exists())

    def test_symlinks_are_foreign(self):
        target = self.tmp / "elsewhere"
        target.mkdir()
        self.claude.parent.parent.mkdir(parents=True)
        self.claude.parent.symlink_to(target)
        self.assertEqual(self.states()["claude"], "foreign")
        self.run_main("skill", "setup")
        self.assertEqual(list(target.iterdir()), [])

    def test_env_overrides_the_folders(self):
        claude_dir, codex_dir = self.tmp / "cc", self.tmp / "cx"
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(claude_dir), "CODEX_HOME": str(codex_dir)}):
            code, out, _ = self.run_main("skill", "status", "--json")
            paths = {r["agent"]: r["path"] for r in json.loads(out)["locations"]}
            self.assertEqual(paths, {"claude": str(claude_dir / "skills/agentbox/SKILL.md"),
                                     "agents": str(self.agents),
                                     "codex": str(codex_dir / "skills/agentbox/SKILL.md")})
            self.run_main("skill", "setup")
        self.assertEqual((claude_dir / "skills/agentbox/SKILL.md").read_text(), ab.SKILL_STUB)
        self.assertEqual((codex_dir / "skills/agentbox/SKILL.md").read_text(), ab.SKILL_STUB)
        self.assertFalse(self.claude.exists())

    def test_json_only_for_status(self):
        code, out, err = self.run_main("skill", "setup", "--json")
        self.assertEqual((code, out), (2, ""))
        self.assertFalse(self.claude.exists())
