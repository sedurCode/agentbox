"""`agentbox setup`: seeds the config dir from share/ and never overwrites what is there."""
import json
import os
from unittest import mock

from helpers import AgentboxTestCase, ab

SHIPPED_KITS = sorted(p.name for p in (ab.SHARE_DIR / "kits").iterdir() if p.is_dir())


class SetupTests(AgentboxTestCase):
    def setup(self, *extra):
        code, out, err = self.run_main("setup", "--json", *extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def actions(self, doc):
        return {(r["kind"], r["name"]): r["action"] for r in doc["items"]}

    def test_fresh_config_dir_gets_everything(self):
        self.assertFalse(ab.CONFIG_FILE.exists())
        doc = self.setup()
        self.assertEqual(doc["version"], ab.VERSION)
        acts = self.actions(doc)
        self.assertEqual(acts[("config", "agentbox.toml")], "created")
        for k in SHIPPED_KITS:
            self.assertEqual(acts[("kit", k)], "created")
            self.assertTrue((ab.KITS_DIR / k / f"{k}.yaml").is_file())
        self.assertEqual(acts[("gdb", "fault-snapshot.gdb")], "created")
        self.assertEqual(ab.CONFIG_FILE.read_text(),
                         (ab.SHARE_DIR / "config" / "agentbox.example.toml").read_text())
        if not ab.IS_WIN:
            self.assertEqual(ab.CONFIG_DIR.stat().st_mode & 0o777, 0o700)
        self.assertTrue(any("agentbox doctor" in s for s in doc["next"]))
        self.assertTrue(any(str(ab.CONFIG_FILE) in s for s in doc["next"]))     # edit the new config

    def test_second_run_changes_nothing(self):
        self.setup()
        before = {p: p.read_bytes() for p in ab.CONFIG_DIR.rglob("*") if p.is_file()}
        doc = self.setup()
        self.assertEqual(set(self.actions(doc).values()), {"kept"})
        self.assertEqual({p: p.read_bytes() for p in ab.CONFIG_DIR.rglob("*") if p.is_file()}, before)
        self.assertFalse(any(str(ab.CONFIG_FILE) in s for s in doc["next"]))

    def test_users_files_are_never_replaced(self):
        ab.CONFIG_FILE.write_text("# mine\n")
        kit = ab.KITS_DIR / SHIPPED_KITS[0]
        kit.mkdir(parents=True)
        (kit / "mine.yaml").write_text("edited\n")
        acts = self.actions(self.setup())
        self.assertEqual(acts[("config", "agentbox.toml")], "kept")
        self.assertEqual(acts[("kit", SHIPPED_KITS[0])], "kept_differs")
        self.assertEqual(ab.CONFIG_FILE.read_text(), "# mine\n")
        self.assertEqual(sorted(p.name for p in kit.iterdir()), ["mine.yaml"])

    def test_v2_kit_is_moved_aside(self):
        kit = ab.KITS_DIR / SHIPPED_KITS[0]
        kit.mkdir(parents=True)
        (kit / "spec.yaml").write_text("v2\n")
        doc = self.setup()
        (row,) = [r for r in doc["items"] if r["name"] == SHIPPED_KITS[0] and r["action"] == "moved_v2"]
        self.assertTrue((ab.KITS_DIR.parent / "kits" / os.path.basename(row["backup"]) / "spec.yaml").is_file())
        self.assertEqual(self.actions(doc)[("kit", SHIPPED_KITS[0])], "created")
        self.assertFalse((kit / "spec.yaml").exists())

    def test_runs_without_a_config_and_prints_a_summary(self):
        code, out, err = self.run_main("setup")
        self.assertEqual(code, 0, err)
        self.assertIn(f"agentbox {ab.VERSION}", out)
        self.assertIn("created", out)
        self.assertIn("next:", out)

    def test_missing_defaults_is_an_error(self):
        empty = self.tmp / "share"
        empty.mkdir()
        with mock.patch.object(ab, "SHARE_DIR", empty):
            code, _, err = self.run_main("setup")
        self.assertNotEqual(code, 0)
        self.assertIn("reinstall", err)
        self.assertFalse(ab.CONFIG_FILE.exists())

    def test_commands_needing_a_config_point_at_setup(self):
        code, _, err = self.run_main("status")
        self.assertNotEqual(code, 0)
        self.assertIn("agentbox setup", err)
