"""`agentbox --version`, and the version in `doctor`."""
import json
import re

from helpers import AgentboxTestCase, FakeSbx, ab


class VersionTests(AgentboxTestCase):
    def test_version_is_semver(self):
        self.assertRegex(ab.VERSION, r"^\d+\.\d+\.\d+$")

    def test_version_flag(self):
        code, out, _ = self.run_main("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), f"agentbox {ab.VERSION}")

    def test_doctor_shows_the_version(self):
        self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
        ''')
        FakeSbx(exists=False).install(self)
        _, out, _ = self.run_main("doctor")
        self.assertRegex(out, rf"agentbox {re.escape(ab.VERSION)} \(Python \d+\.\d+")
        self.assertIn(str(ab.SCRIPT), out)             # the stable path (Homebrew: opt/, not the versioned Cellar)
        _, out, _ = self.run_main("doctor", "--json")
        self.assertEqual(json.loads(out)["version"], ab.VERSION)
