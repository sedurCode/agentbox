"""share/vscode: the VS Code extension. The manifest and the commands it runs are checked here; model.js runs
under a real `node` (skipped without it)."""
import json
import re
import shutil
import sys
import unittest

from unittest import mock

from helpers import REAL_RUN, ROOT, AgentboxTestCase, ab

EXT = ROOT / "share" / "vscode"


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.pkg = json.loads((EXT / "package.json").read_text())
        self.src = re.sub(r"^\s*//.*$", "", (EXT / "extension.js").read_text(), flags=re.M)   # code, not comments

    def test_every_contributed_command_is_registered(self):
        ids = {c["command"] for c in self.pkg["contributes"]["commands"]}
        registered = set(re.findall(r'cmd\("(agentbox\.[A-Za-z]+)"', self.src))
        self.assertEqual(ids, registered)
        for menu in self.pkg["contributes"]["menus"].values():
            for item in menu:
                self.assertIn(item["command"], ids)

    def test_runs_only_the_dashboards_verbs_and_never_destructive_flags(self):
        verbs = set(re.findall(r'\[\s*"([a-z]+)", name', self.src)) | set(re.findall(r'\["(dashboard)", "--', self.src))
        self.assertEqual(verbs, {"progress", "logs", "fetch", "up", "stop", "review", "code", "dashboard"})
        for bad in ("--force", "--start", '"down"', '"fresh"', '"run"', '"resume"'):
            self.assertNotIn(bad, self.src)

    def test_files_exist(self):
        self.assertTrue((EXT / self.pkg["main"]).is_file())
        icon = self.pkg["contributes"]["viewsContainers"]["activitybar"][0]["icon"]
        self.assertTrue((EXT / icon).is_file())


class PackTests(unittest.TestCase):
    def test_vsix_holds_the_extension_and_a_manifest(self):
        import tempfile, zipfile
        from xml.dom import minidom
        with tempfile.TemporaryDirectory() as d:
            vsix = ab.pack_vsix(d, EXT)
            with zipfile.ZipFile(vsix) as z:
                names = set(z.namelist())
                self.assertEqual(names, {"[Content_Types].xml", "extension.vsixmanifest"}
                                 | {f"extension/{f}" for f in ab.VSCODE_EXT_FILES})
                ident = minidom.parseString(z.read("extension.vsixmanifest")).getElementsByTagName("Identity")[0]
                self.assertEqual(ident.getAttribute("Publisher") + "." + ident.getAttribute("Id"), "sedaudio.agentbox")
                minidom.parseString(z.read("[Content_Types].xml"))

    def test_every_file_the_extension_needs_is_packed(self):
        shipped = {str(p.relative_to(EXT)).replace("\\", "/") for p in EXT.rglob("*") if p.is_file()}
        self.assertEqual(shipped, set(ab.VSCODE_EXT_FILES))


class CommandTests(AgentboxTestCase):
    """`agentbox vscode`: `code` is stubbed; nothing touches the real VS Code."""

    def fake_code(self, listed=""):
        calls = []
        def run(cmd, **kw):
            calls.append(cmd[1:])
            return mock.Mock(returncode=0, stdout=listed if "--list-extensions" in cmd else "")
        for p in (mock.patch.object(ab, "find_vscode", lambda cfg: "/x/code"),
                  mock.patch.object(ab.shutil, "which", lambda c: "/x/code"),
                  mock.patch.object(ab.subprocess, "run", run)):
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_status(self):
        self.fake_code("ms-python.python@1.0\nsedaudio.agentbox@0.0.9\n")
        code, out, _ = self.run_main("vscode", "status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["state"], "outdated")

    def test_install_packs_and_installs_with_force(self):
        calls = self.fake_code()
        code, out, err = self.run_main("vscode", "install")
        self.assertEqual(code, 0, err)
        inst = [c for c in calls if c[0] == "--install-extension"]
        self.assertEqual(len(inst), 1)
        self.assertTrue(inst[0][1].endswith(".vsix") and inst[0][2] == "--force")

    def test_uninstall_only_when_installed(self):
        calls = self.fake_code()
        self.assertEqual(self.run_main("vscode", "uninstall")[0], 0)
        self.assertFalse(any(c[0] == "--uninstall-extension" for c in calls))
        calls = self.fake_code("sedaudio.agentbox@0.1.0\n")
        self.run_main("vscode", "uninstall")
        self.assertIn(["--uninstall-extension", "sedaudio.agentbox"], calls)

    def test_json_only_for_status(self):
        self.assertEqual(self.run_main("vscode", "install", "--json")[0], 2)


@unittest.skipUnless(shutil.which("node"), "needs node")
class ModelTests(unittest.TestCase):
    def test_model(self):
        r = REAL_RUN(["node", str(ROOT / "tests" / "fixtures" / "vscode_model_test.js"), str(EXT / "model.js")],
                     capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")
