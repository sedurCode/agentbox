"""`permission_mode` for Claude profiles: validation, the headless and interactive argv, the startup check
that a job really runs in auto mode, and counting the actions Claude Code's permission check refused."""
import argparse
import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from helpers import AgentboxTestCase, FakeSbx, ab, needs_linux_vm
from test_jobs import PROMPT, JobCase

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "claude-denials.jsonl"
JOB = "20261123T164235Z-dbb67c"


class ValidationTests(AgentboxTestCase):
    def cfg(self, body):
        return self.write_config(body.replace("WS", str(self.ws)))

    def test_default_is_auto_for_claude(self):
        prof = self.cfg('[profiles.p]\nworkspace = "WS"\n').profile("p")
        self.assertEqual(prof.permission_mode, "auto")

    def test_bypass_and_defaults(self):
        cfg = self.cfg('[defaults]\npermission_mode = "bypass"\n[profiles.p]\nworkspace = "WS"\n'
                       '[profiles.q]\nworkspace = "WS"\npermission_mode = "auto"\n'
                       '[profiles.o]\nagent = "opencode"\nworkspace = "WS"\n')
        self.assertEqual(cfg.profile("p").permission_mode, "bypass")
        self.assertEqual(cfg.profile("q").permission_mode, "auto")            # the profile wins
        self.assertIsNone(cfg.profile("o").permission_mode)                    # [defaults] only reaches Claude

    def test_bad_values_are_refused(self):
        for bad in ('"bypassPermissions"', '"Auto"', '"plan"', '""', "true", "1", '["auto"]'):
            cfg = self.cfg(f'[profiles.p]\nworkspace = "WS"\npermission_mode = {bad}\n')
            with self.subTest(bad=bad), self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                cfg.profile("p")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn('permission_mode must be "auto"', err.getvalue())

    def test_bad_value_in_defaults_is_refused(self):
        cfg = self.cfg('[defaults]\npermission_mode = "yolo"\n[profiles.p]\nworkspace = "WS"\n')
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cfg.profile("p")

    def test_refused_on_other_agents(self):
        for agent in ("opencode", "codex", "shell"):
            cfg = self.cfg(f'[profiles.p]\nagent = "{agent}"\nworkspace = "WS"\npermission_mode = "auto"\n')
            with self.subTest(agent=agent), self.assertRaises(SystemExit) as cm, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                cfg.profile("p")
            self.assertEqual(cm.exception.code, 2)
            self.assertIn('only for agent = "claude"', err.getvalue())

    def test_doctor_reports_instead_of_crashing(self):
        self.cfg('[profiles.p]\nagent = "opencode"\nworkspace = "WS"\npermission_mode = "auto"\n'
                 '[profiles.q]\nworkspace = "WS"\npermission_mode = "nope"\n')
        FakeSbx().install(self)
        code, out, _ = self.run_main("doctor", "--json")
        self.assertEqual(code, 1)
        fails = {c["profile"]: c["message"] for c in json.loads(out)["checks"] if c["status"] == "fail"}
        self.assertIn('only for agent = "claude"', fails["p"])
        self.assertIn('must be "auto"', fails["q"])


class ArgvTests(AgentboxTestCase):
    def prof(self, mode=None, agent="claude"):
        extra = f'permission_mode = "{mode}"\n' if mode else ""
        cfg = self.write_config(f'[profiles.p]\nagent = "{agent}"\nworkspace = "{self.ws}"\n{extra}')
        return cfg.profile("p")

    def test_headless_auto(self):
        argv = ab.headless_cmd(self.prof(), ab.HEADLESS_AGENTS["claude"])
        self.assertEqual(argv, ["claude", "-p", "--output-format", "stream-json", "--verbose",
                                "--permission-mode", "auto"])

    def test_headless_bypass(self):
        argv = ab.headless_cmd(self.prof("bypass"), ab.HEADLESS_AGENTS["claude"])
        self.assertEqual(argv, ["claude", "-p", "--output-format", "stream-json", "--verbose",
                                "--dangerously-skip-permissions"])

    def test_opencode_is_untouched(self):
        argv = ab.headless_cmd(self.prof(agent="opencode"), ab.HEADLESS_AGENTS["opencode"])
        self.assertEqual(argv, ["opencode", "run", "--format", "json", "--auto"])

    def runs(self, prof, fn, *a):
        seen = []
        with mock.patch.object(ab, "sbx", lambda cfg, *args, **k: (seen.append(args), subprocess.CompletedProcess(args, 0, "", ""))[1]), \
                mock.patch.object(ab, "ensure_sandbox", lambda p, retry=False: None), \
                mock.patch.object(ab, "sync_skills", lambda p, background=True: None), \
                contextlib.redirect_stdout(io.StringIO()):
            fn(prof, *a)
        return [c for c in seen if c[0] == "run"]

    def test_up_passes_the_mode_after_the_separator(self):
        for mode, flag in ((None, "--permission-mode=auto"), ("auto", "--permission-mode=auto"),
                           ("bypass", "--dangerously-skip-permissions")):
            prof = self.prof(mode)
            self.assertEqual(self.runs(prof, ab.cmd_up, argparse.Namespace(agent_args=[])),
                             [("run", "--name", "ab-p", "--", flag)])
            self.assertEqual(self.runs(prof, ab.cmd_up, argparse.Namespace(agent_args=["--model", "x"])),
                             [("run", "--name", "ab-p", "--", flag, "--model", "x")])

    def test_up_for_other_agents_adds_nothing(self):
        self.assertEqual(self.runs(self.prof(agent="opencode"), ab.cmd_up, argparse.Namespace(agent_args=[])),
                         [("run", "--name", "ab-p")])

    def test_resume(self):
        for mode, flag in (("auto", "--permission-mode=auto"), ("bypass", "--dangerously-skip-permissions")):
            prof = self.prof(mode)
            done = {"id": JOB, "state": "finished", "session_id": "sess-12345678", "vm": "running"}
            with mock.patch.object(ab, "sandbox_exists", lambda p, retry=False: True), \
                    mock.patch.object(ab, "job_info", lambda p, j=None, states=None: dict(ab.NO_JOB) | done), \
                    mock.patch.object(ab, "resolve_job", lambda p, j: JOB):
                runs = self.runs(prof, ab.cmd_resume, argparse.Namespace(job=None))
            self.assertEqual(runs, [("run", "--name", "ab-p", "--", flag, "--resume=sess-12345678")])
            self.assertTrue(all("=" in a or not a.startswith("--permission") for a in runs[0]))


@needs_linux_vm
class RunModeCheckTests(JobCase):
    def run_job(self, **env):
        self.vm.env.update(env)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            try:
                code = ab.cmd_run(self.prof, self.args())
            except SystemExit as e:
                code = e.code
        return code, err.getvalue()

    def test_auto_argv_on_the_job_and_prompt_still_on_stdin(self):
        argv_file = self.tmp / "argv"
        code, _ = self.run_job(FAKE_CLAUDE_MODE="auto", FAKE_CLAUDE_ARGV=str(argv_file))
        self.assertEqual(code, 0)
        self.finish(ab.job_record(self.prof)["id"])
        self.assertEqual(argv_file.read_text().split("\n")[:-1],
                         ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "auto"])
        for call in self.vm.calls:
            self.assertNotIn("SECRET-PROMPT-TEXT", " ".join(map(str, call)))
        self.assertNotIn("--dangerously-skip-permissions", argv_file.read_text())

    def test_bypass_argv(self):
        (self.cfg_dir / "agentbox.toml").write_text((self.cfg_dir / "agentbox.toml").read_text()
                                                    .replace('[profiles.p]\n', '[profiles.p]\npermission_mode = "bypass"\n', 1))
        self.prof = ab.Config().profile("p")
        argv_file = self.tmp / "argv"
        code, _ = self.run_job(FAKE_CLAUDE_MODE="bypassPermissions", FAKE_CLAUDE_ARGV=str(argv_file))
        self.assertEqual(code, 0)                         # bypass: no auto-mode check
        self.finish(ab.job_record(self.prof)["id"])
        self.assertEqual(argv_file.read_text().split("\n")[:-1],
                         ["-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"])

    def test_refused_auto_mode_stops_the_job_and_says_bypass(self):
        code, err = self.run_job(FAKE_CLAUDE_MODE="default", FAKE_CLAUDE_SLEEP="30")
        self.assertEqual(code, ab.EXIT_FAIL)
        self.assertIn("'default' mode, not auto mode", err)
        self.assertIn('permission_mode = "bypass"', err)
        self.assertNotEqual(ab.job_info(self.prof)["state"], "running")       # stopped, not left running
        self.assertEqual(self.keepalive, [])

    def test_unknown_or_missing_mode_carries_on(self):
        for env in ({"FAKE_CLAUDE_MODE": "somethingNew"}, {}):
            self.vm.env.pop("FAKE_CLAUDE_MODE", None)
            code, _ = self.run_job(**env)
            self.assertEqual(code, 0, env)
            self.finish(ab.job_record(self.prof)["id"])

    def test_old_claude_usage_error_fails_run_clearly(self):
        msg = "error: option '--permission-mode <mode>' argument 'auto' is invalid. Allowed choices are default, plan."
        code, err = self.run_job(FAKE_CLAUDE_USAGE_ERROR=msg)
        self.assertEqual(code, ab.EXIT_FAIL)
        self.assertIn("did not accept auto mode", err)
        self.assertIn("argument 'auto' is invalid", err)
        self.assertIn('permission_mode = "bypass"', err)

    def test_an_explicit_mode_in_agent_args_is_not_second_guessed(self):
        self.vm.env["FAKE_CLAUDE_MODE"] = "acceptEdits"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = ab.cmd_run(self.prof, self.args(agent_args=["--permission-mode", "acceptEdits"]))
        self.assertEqual(code, 0)
        self.finish(ab.job_record(self.prof)["id"])

    def test_other_quick_failures_are_left_to_logs_and_wait(self):
        code, _ = self.run_job(FAKE_CLAUDE_USAGE_ERROR="error: not logged in")
        self.assertEqual(code, 0)

    def test_mode_check_never_hangs(self):
        with mock.patch.object(ab, "vm_exec", lambda prof, script, stdin=None:
                               subprocess.CompletedProcess([], 0, "TIMEOUT\n", "")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            ab.check_auto_mode(self.prof, JOB)
        self.assertIn("could not confirm auto mode", err.getvalue())
        self.assertIn("while [ $i -lt 150 ]", ab.mode_check_script(JOB))   # bounded: 30 s at 0.2 s


class DenialTests(unittest.TestCase):
    def acc(self):
        acc = ab.new_acc(JOB)
        ab.ingest(acc, FIXTURE.read_text())
        return acc

    def test_denials_are_counted_once_each(self):
        acc = self.acc()
        self.assertEqual(acc["denials"], 2)          # the repeat, the plain error and the quoted text don't count
        self.assertEqual((acc["turns"], acc["tool_calls"]), (2, 3))

    def test_chunked_ingest_gives_the_same_count(self):
        acc = ab.new_acc(JOB)
        for line in FIXTURE.read_text().splitlines(keepends=True):
            ab.ingest(acc, line)
        self.assertEqual(acc["denials"], 2)

    def test_old_cache_without_denials_is_still_usable(self):
        acc = self.acc()
        del acc["denials"]
        self.assertTrue(ab.acc_ok(acc, JOB))
        self.assertEqual(acc["denials"], 0)

    def test_shown_in_progress_status_and_logs(self):
        prof = mock.Mock(d={"stall_after": 300})
        j = {"id": JOB, "state": "running", "started_at": None, "finished_at": None}
        view = ab.progress_view(prof, self.acc(), j, {}, 1e9)
        self.assertEqual(view["denials"], 2)
        self.assertTrue(any(ln.startswith("DENIED 2:") for ln in ab.progress_lines(JOB, "running", view)))
        self.assertIn("2 denied", ab.progress_suffix(view))
        rendered = [ab.render_stream_line(ln) for ln in FIXTURE.read_text().splitlines()]
        self.assertIn("mode auto", rendered[0])
        denied = [ln for r in rendered if r for ln in r.split("\n") if ln.startswith("denied: ")]
        self.assertEqual(len(denied), 3)              # the log shows every event, repeats included
        view0 = ab.progress_view(prof, ab.new_acc(JOB), j, {}, 1e9)
        self.assertEqual(view0["denials"], 0)
        self.assertFalse(any(ln.startswith("DENIED") for ln in ab.progress_lines(JOB, "running", view0)))
        self.assertNotIn("denied", ab.progress_suffix(view0))

    def test_shown_in_the_dashboard(self):
        from test_dashboard_interactive import NOW, job, row, text_of
        prof = mock.Mock(d={"stall_after": 300})
        view = ab.progress_view(prof, self.acc(), {"id": JOB, "state": "running"}, {}, NOW)
        text = text_of(ab.dash_detail(row("a", j=job(progress=view)), ab.DashHistory(), 100, NOW, False))
        self.assertIn("denied  2", text)

    def test_opencode_streams_are_unaffected(self):
        acc = ab.new_acc(JOB)
        ab.ingest(acc, (FIXTURE.parent / "opencode-run.jsonl").read_text())
        self.assertEqual(acc["denials"], 0)


if __name__ == "__main__":
    unittest.main()
