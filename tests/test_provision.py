import io
import json
import os
import sys
import tomllib
import unittest
from unittest import mock

from helpers import REAL_RUN, AgentboxTestCase, FakeSbx, ab, real_sh, needs_hostile_filenames, needs_mkfifo, posix_only


class Tty(io.StringIO):
    def isatty(self):
        return True


class ProvisionCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'''
            # my comment survives
            [defaults]
            deny = ["*.corp"]

            [profiles.old]   # trailing comment
            workspace = "{self.ws}"
        ''')
        self.conf = self.cfg_dir / "agentbox.toml"
        for kit in ("python-node", "embedded-arm", "claude-team"):
            (self.cfg_dir / "kits" / kit).mkdir(parents=True)
            (self.cfg_dir / "kits" / kit / f"{kit}.yaml").write_text(ab.KIT_SYNTAX + "\nkind: mixin\n")
        self.proj = self.tmp / "proj"
        self.proj.mkdir()
        self.fake = FakeSbx(exists=False).install(self)
        p = mock.patch.object(sys, "stdin", io.StringIO())
        p.start()
        self.addCleanup(p.stop)

    def prov(self, name="new", *argv, expect=0):
        code, out, err = self.run_main("provision", name, *argv)
        self.assertEqual(code, expect, out + err)
        return out, err

    def profile(self, name="new"):
        return tomllib.loads(self.conf.read_text(encoding="utf-8"))["profiles"][name]

    def backups(self):
        return sorted(self.cfg_dir.glob("agentbox.toml.bak-*"))


class FlagOnlyTests(ProvisionCase):
    @posix_only
    def test_yes_with_flags_appends_and_keeps_the_file(self):
        (self.proj / "pyproject.toml").write_text("[project]\n")
        before = self.conf.read_bytes()
        out, _ = self.prov("new", "--yes", "--workspace", str(self.proj))
        after = self.conf.read_bytes()
        self.assertTrue(after.startswith(before + b"\n"))                   # appended after a blank line
        self.assertIn(b"# my comment survives", after)
        p = self.profile()
        self.assertEqual((p["agent"], p["workspace"], p["mode"]), ("claude", str(self.proj), "clone"))
        self.assertEqual(p["kits"], ["python-node", "claude-team"])
        self.assertEqual(p["allow"], ["pypi.org", "files.pythonhosted.org"])
        self.assertNotIn("host_tools", p)
        (bak,) = self.backups()
        self.assertEqual(bak.read_bytes(), before)
        self.assertEqual(oct(bak.stat().st_mode & 0o777), "0o600")
        self.assertIn("agentbox up new", out)
        self.assertIn("host_tools are not set up", out)
        self.assertIn(("version",), self.fake.calls)                         # doctor ran in process
        self.assertIn("all good", out)

    def test_explicit_flags_replace_suggestions(self):
        (self.proj / "pyproject.toml").write_text("")
        self.prov("new", "--yes", "--workspace", str(self.proj), "--agent", "shell", "--mode", "direct",
                  "--kit", "embedded-arm", "--allow", "example.com", "--extra", f"{self.ws}:ro",
                  "--instructions", "be careful")
        p = self.profile()
        self.assertEqual((p["agent"], p["mode"], p["kits"], p["allow"]), ("shell", "direct", ["embedded-arm"], ["example.com"]))
        self.assertEqual(p["extra_workspaces"], [f"{self.ws}:ro"])
        self.assertEqual(p["instructions"], "be careful")

    def test_defaults_are_not_repeated(self):
        self.write_config(f'[defaults]\nkits = ["claude-team"]\nallow = ["pypi.org"]\nskills = []\n'
                          f'[profiles.old]\nworkspace = "{self.ws}"\n')
        (self.proj / "requirements-dev.txt").write_text("")
        self.prov("new", "--yes", "--workspace", str(self.proj))
        p = self.profile()
        self.assertEqual((p["kits"], p["allow"]), (["python-node"], ["files.pythonhosted.org"]))

    def test_skills_by_flag_and_unknown_set(self):
        sk = self.tmp / "skills"
        (sk / "tdd").mkdir(parents=True)
        (sk / "tdd" / "SKILL.md").write_text("x")
        self.write_config(f'[skillsets.s]\npath = "{sk}"\n[profiles.old]\nworkspace = "{self.ws}"\n')
        self.prov("new", "--yes", "--workspace", str(self.proj), "--skills", "s")
        self.assertEqual(self.profile()["skills"], ["s"])
        self.prov("two", "--yes", "--workspace", str(self.proj), "--skills", "nope", expect=2)

    def test_up_starts_only_when_doctor_is_happy(self):
        started = []
        with mock.patch.object(ab, "cmd_up", lambda prof, a: started.append(prof.name) or 0):
            self.prov("new", "--yes", "--workspace", str(self.proj), "--up")
        self.assertEqual(started, ["new"])


class DetectionTests(ProvisionCase):
    def detect(self, agent="claude", defaults=None):
        found = ab.detect_workspace(self.proj)
        kits, allow = ab.suggest_settings(found, agent, defaults or {})
        return [k for k, _ in kits], [h for h, _ in allow]

    def test_nothing_found(self):
        self.assertEqual(self.detect(), (["claude-team"], []))
        self.assertEqual(self.detect("codex"), ([], []))

    def test_python_files(self):
        for f in ("pyproject.toml", "setup.py", "requirements.txt", "requirements-dev.txt"):
            for old in self.proj.iterdir():
                old.unlink()
            (self.proj / f).write_text("")
            self.assertEqual(self.detect("codex"), (["python-node"], ["pypi.org", "files.pythonhosted.org"]), f)

    def test_node(self):
        (self.proj / "package.json").write_text("{}")
        self.assertEqual(self.detect("codex"), (["python-node"], ["registry.npmjs.org"]))

    def test_arm_from_cmake_makefile_and_linker_scripts(self):
        (self.proj / "CMakeLists.txt").write_text("set(CMAKE_C_COMPILER arm-none-eabi-gcc)")
        self.assertEqual(self.detect("codex")[0], ["embedded-arm"])
        (self.proj / "CMakeLists.txt").unlink()
        (self.proj / "Makefile").write_text("CC=arm-none-eabi-gcc")
        self.assertEqual(self.detect("codex")[0], ["embedded-arm"])
        (self.proj / "Makefile").write_text("CC=gcc")
        self.assertEqual(self.detect("codex")[0], [])
        (self.proj / "ld").mkdir()
        (self.proj / "ld" / "STM32.ld").write_text("")
        self.assertEqual(self.detect("codex")[0], ["embedded-arm"])

    def test_linker_scripts_in_skipped_dirs_do_not_count(self):
        (self.proj / "node_modules").mkdir()
        (self.proj / "node_modules" / "x.ld").write_text("")
        self.assertEqual(self.detect("codex")[0], [])

    def test_github_hosts_only_for_git_urls(self):
        (self.proj / "requirements.txt").write_text("pkg @ git+https://github.com/o/r.git@v1\n")
        kits, allow = self.detect("codex")
        self.assertEqual(allow, ["pypi.org", "files.pythonhosted.org", *ab.GITHUB_HOSTS])
        (self.proj / "requirements.txt").write_text("requests\n")
        self.assertNotIn("github.com", self.detect("codex")[1])
        (self.proj / "package-lock.json").write_text('{"resolved": "git+https://github.com/o/r.git#abc"}')
        self.assertIn("codeload.github.com", self.detect("codex")[1])

    def test_kits_that_are_not_installed_are_not_suggested(self):
        import shutil
        shutil.rmtree(self.cfg_dir / "kits" / "python-node")
        (self.proj / "pyproject.toml").write_text("")
        self.assertEqual(self.detect()[0], ["claude-team"])

    def test_every_suggestion_has_a_reason(self):
        (self.proj / "pyproject.toml").write_text("")
        kits, allow = ab.suggest_settings(ab.detect_workspace(self.proj), "claude", {})
        self.assertTrue(all(r for _, r in kits + allow))

    @needs_mkfifo
    def test_never_runs_anything_from_the_workspace(self):
        (self.proj / "pyproject.toml").write_text("")
        (self.proj / "fifo").touch()
        os.mkfifo(self.proj / "requirements.txt")                           # reading it would hang
        ab.detect_workspace(self.proj)                                       # subprocess is forbidden in tests
        self.assertEqual(ab._read_small(self.proj / "requirements.txt"), "")


class RefusalTests(ProvisionCase):
    def test_duplicate_and_bad_names(self):
        self.prov("old", "--yes", "--workspace", str(self.proj), expect=2)
        for bad in ("Bad", "-x", "a" * 42, "has space", "x_y"):
            self.prov(bad, "--yes", "--workspace", str(self.proj), expect=2)
        self.assertEqual(self.backups(), [])

    def test_name_clash_with_an_existing_sandbox(self):
        FakeSbx(exists=True, names=("ab-new",)).install(self)
        _, err = self.prov("new", "--yes", "--workspace", str(self.proj), expect=2)
        self.assertIn("ab-new already exists", err)

    def test_missing_or_unsuitable_workspace(self):
        _, err = self.prov("new", "--yes", "--workspace", str(self.tmp / "nope"), expect=2)
        self.assertIn("not a folder", err)
        (self.tmp / "afile").write_text("")
        self.prov("new", "--yes", "--workspace", str(self.tmp / "afile"), expect=2)

    def test_workspace_that_contains_the_config_kits_state_or_claude_dir(self):
        for ws in (self.tmp, self.cfg_dir, self.cfg_dir / "kits"):
            _, err = self.prov("new", "--yes", "--workspace", str(ws), expect=2)
            self.assertRegex(err, "contains|is inside")
        self.state_dir.mkdir(exist_ok=True)
        self.prov("new", "--yes", "--workspace", str(self.state_dir), expect=2)
        fake_home = self.tmp / "home"
        (fake_home / ".claude").mkdir(parents=True)
        with mock.patch.object(ab, "HOME", fake_home):
            _, err = self.prov("new", "--yes", "--workspace", str(fake_home), expect=2)
            self.assertIn("~/.claude", err)
        self.assertEqual(self.backups(), [])

    def test_no_default_workspace_outside_a_git_tree(self):
        with mock.patch.object(ab.Path, "cwd", return_value=self.proj):
            _, err = self.prov("new", "--yes", expect=2)
        self.assertIn("--workspace", err)

    def test_the_current_folder_is_the_default_in_a_git_tree(self):
        (self.proj / ".git").mkdir()
        with mock.patch.object(ab.Path, "cwd", return_value=self.proj):
            self.prov("new", "--yes")
        self.assertEqual(self.profile()["workspace"], str(self.proj))

    def test_unknown_agent_kit_and_bad_host(self):
        self.prov("new", "--yes", "--workspace", str(self.proj), "--agent", "nope", expect=2)
        self.prov("new", "--yes", "--workspace", str(self.proj), "--kit", "nope", expect=2)
        self.prov("new", "--yes", "--workspace", str(self.proj), "--allow", 'evil"host', expect=2)
        self.prov("new", "--yes", "--workspace", str(self.proj), "--extra", str(self.tmp), expect=2)

    def test_no_tty_without_yes_refuses(self):
        _, err = self.prov("new", "--workspace", str(self.proj), expect=2)
        self.assertIn("--yes", err)
        self.prov("new", "--workspace", str(self.proj), "--dry-run", expect=2)
        self.assertEqual(self.backups(), [])

    def test_instructions_with_a_literal_string_terminator_or_controls(self):
        for bad in ("a ''' b", "esc\x1b[2J", "nul\x00"):
            self.prov("new", "--yes", "--workspace", str(self.proj), "--instructions", bad, expect=2)
        self.assertEqual(self.backups(), [])


class EscapingTests(ProvisionCase):
    def test_toml_string_helper(self):
        for s in ('plain', 'quo"te', 'back\\slash', 'new\nline', 'tab\t', 'uni ü 😀 日本', 'del\x7f', "ctl\x01\x1f", ""):
            self.assertEqual(tomllib.loads("k = " + ab.toml_str(s))["k"], s, repr(s))
        with self.assertRaises(ValueError):
            ab.toml_str("bad \udcff surrogate")

    @needs_hostile_filenames
    def test_instructions_and_paths_survive_a_round_trip(self):
        weird = self.tmp / 'we"ird \\ ü😀 dir'
        weird.mkdir()
        instr = 'Say "hi" \\n not an escape\nline two with \'single\' quotes and a tab\t and ü 😀\n\nend\''
        self.prov("new", "--yes", "--workspace", str(weird), "--extra", f"{weird}:ro", "--instructions", instr)
        p = self.profile()
        self.assertEqual(p["workspace"], str(weird))
        self.assertEqual(p["extra_workspaces"], [f"{weird}:ro"])
        self.assertEqual(p["instructions"], instr)

    def test_crlf_instructions_become_lf(self):
        self.prov("new", "--yes", "--workspace", str(self.proj), "--instructions", "a\r\nb")
        self.assertEqual(self.profile()["instructions"], "a\nb")

    def test_a_file_without_a_final_newline(self):
        self.conf.write_bytes(self.conf.read_bytes().rstrip(b"\n"))
        self.prov("new", "--yes", "--workspace", str(self.proj))
        self.assertIn("new", tomllib.loads(self.conf.read_text())["profiles"])

    def test_comments_cannot_inject_toml(self):
        self.assertNotIn("\n", ab.toml_comment("a\nb = 1"))
        self.assertNotIn("\x1b", ab.toml_comment("a\x1b[2J"))


class SafetyTests(ProvisionCase):
    def test_a_block_that_does_not_parse_restores_the_file(self):
        before = self.conf.read_bytes()
        with mock.patch.object(ab, "profile_block", lambda *a: "[profiles.new\nbroken = \n"):
            _, err = self.prov("new", "--yes", "--workspace", str(self.proj), expect=1)
        self.assertEqual(self.conf.read_bytes(), before)
        self.assertIn("restored", err)
        (bak,) = self.backups()
        self.assertEqual(bak.read_bytes(), before)

    def test_a_block_that_reads_back_differently_restores_the_file(self):
        before = self.conf.read_bytes()
        real = ab.profile_block
        with mock.patch.object(ab, "profile_block",
                               lambda n, d, notes: real(n, {**d, "workspace": "/somewhere/else"}, notes)):
            self.prov("new", "--yes", "--workspace", str(self.proj), expect=1)
        self.assertEqual(self.conf.read_bytes(), before)

    def test_a_profile_that_fails_the_normal_checks_restores_the_file(self):
        before = self.conf.read_bytes()
        with mock.patch.object(ab.Config, "profile", side_effect=SystemExit(2)):
            self.prov("new", "--yes", "--workspace", str(self.proj), expect=1)
        self.assertEqual(self.conf.read_bytes(), before)

    def test_dry_run_leaves_the_file_byte_identical(self):
        before = self.conf.read_bytes()
        out, _ = self.prov("new", "--yes", "--dry-run", "--workspace", str(self.proj))
        self.assertEqual(self.conf.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.assertIn("[profiles.new]", out)
        self.assertIn("nothing was changed", out)
        self.assertEqual(self.fake.calls, [("ls", "--json")])               # no doctor, no sandbox

    def test_two_provisions_in_one_second_keep_both_backups(self):
        self.prov("one", "--yes", "--workspace", str(self.proj))
        self.prov("two", "--yes", "--workspace", str(self.proj))
        self.assertEqual(len(self.backups()), 2)


class WarningTests(ProvisionCase):
    def warnings(self):
        return "\n".join(ab.agent_config_warnings(self.proj))

    def test_settings_with_and_without_hooks(self):
        (self.proj / ".claude").mkdir()
        (self.proj / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"command": "x"}]}}))
        (self.proj / ".claude" / "settings.local.json").write_text('{"permissions": {}}')
        w = self.warnings()
        self.assertRegex(w, r"settings\.json: DEFINES HOOKS")
        self.assertRegex(w, r"settings\.local\.json: no hooks")
        (self.proj / ".claude" / "settings.json").write_text("{not json")
        self.assertIn("could not be parsed", self.warnings())

    def test_agent_instruction_mcp_and_vscode_files(self):
        for n in ("CLAUDE.md", "AGENTS.md", ".mcp.json"):
            (self.proj / n).write_text("{}")
        (self.proj / ".vscode").mkdir()
        (self.proj / ".vscode" / "tasks.json").write_text("{}")
        (self.proj / ".vscode" / "launch.json").write_text("{}")
        w = self.warnings()
        for needle in ("CLAUDE.md", "AGENTS.md", ".mcp.json", "tasks.json", "launch.json"):
            self.assertIn(needle, w)

    def test_git_hooks_skip_samples_and_hookspath_is_flagged(self):
        hooks = self.proj / ".git" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit.sample").write_text("")
        self.assertNotIn("hooks", self.warnings())
        (hooks / "pre-commit").write_text("#!/bin/sh\n")
        self.assertIn("pre-commit", self.warnings())
        self.assertNotIn("pre-commit.sample", self.warnings())
        (self.proj / ".git" / "config").write_text("[core]\n\thooksPath = .githooks\n")
        self.assertIn("core.hookspath", self.warnings())
        self.assertIn("overrides", self.warnings())

    @needs_hostile_filenames
    def test_warnings_are_printed_not_blocking_and_escaped(self):
        (self.proj / ".git" / "hooks").mkdir(parents=True)
        (self.proj / ".git" / "hooks" / "evil\x1b[2Jhook").write_text("")
        (self.proj / "CLAUDE.md").write_text("x")
        out, _ = self.prov("new", "--yes", "--workspace", str(self.proj))
        self.assertIn("WARNING", out)
        self.assertIn("CLAUDE.md", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("new", tomllib.loads(self.conf.read_text())["profiles"])


class InteractiveTests(ProvisionCase):
    def run_tty(self, answers, *argv):
        asked = []

        def fake_input(prompt=""):
            asked.append(prompt)
            for key, val in answers.items():
                if key in prompt:
                    return val
            return ""
        with mock.patch.object(sys, "stdin", Tty()), mock.patch("builtins.input", fake_input):
            code, out, err = self.run_main("provision", "new", *argv)
        return code, out, err, asked

    def test_enter_accepts_every_default(self):
        (self.proj / "pyproject.toml").write_text("")
        code, out, err, asked = self.run_tty({"Add this profile": "y"}, "--workspace", str(self.proj))
        self.assertEqual(code, 0, err)
        p = self.profile()
        self.assertEqual((p["mode"], p["kits"], p["allow"]), ("clone", ["python-node", "claude-team"],
                                                               ["pypi.org", "files.pythonhosted.org"]))
        self.assertIn("suggest python-node: pyproject.toml found", out)
        self.assertTrue(any("Add this profile? [y/N]" in a for a in asked))

    def test_default_answer_to_the_question_is_no(self):
        before = self.conf.read_bytes()
        code, out, _, _ = self.run_tty({}, "--workspace", str(self.proj))
        self.assertEqual(code, 0)
        self.assertEqual(self.conf.read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_answers_replace_defaults_and_host_tools_pick_direct(self):
        (self.ws / "x").mkdir()
        code, out, err, _ = self.run_tty({"host tools": "y", "Kits": "none", "Allow": "example.com, other.org",
                                          "Extra folders": f"{self.ws}:ro", "Instructions": "hello",
                                          "Add this profile": "yes"}, "--workspace", str(self.proj))
        self.assertEqual(code, 0, err)
        p = self.profile()
        self.assertEqual((p["mode"], p.get("kits"), p["allow"]), ("direct", None, ["example.com", "other.org"]))
        self.assertEqual((p["extra_workspaces"], p["instructions"]), ([f"{self.ws}:ro"], "hello"))
        self.assertIn("audit new", out)                                      # direct mode is explained

    def test_a_bad_answer_asks_again(self):
        seq = iter(["nope", "", "", "", "", "", "", "n"])
        with mock.patch.object(sys, "stdin", Tty()), mock.patch("builtins.input", lambda p="": next(seq)):
            code, out, _ = self.run_main("provision", "new", "--workspace", str(self.proj), "--mode", "clone")
        self.assertEqual(code, 0)

    def test_skill_sets_are_offered_with_their_count(self):
        sk = self.tmp / "skills"
        for n in ("a", "b"):
            (sk / n).mkdir(parents=True)
            (sk / n / "SKILL.md").write_text("x")
        self.write_config(f'[skillsets.s]\npath = "{sk}"\n[profiles.old]\nworkspace = "{self.ws}"\n')
        code, out, err, _ = self.run_tty({"Skill sets": "s", "Add this profile": "y"}, "--workspace", str(self.proj))
        self.assertEqual(code, 0, err)
        self.assertIn("available s: 2 skill(s)", out)
        self.assertEqual(self.profile()["skills"], ["s"])


if __name__ == "__main__":
    unittest.main()
