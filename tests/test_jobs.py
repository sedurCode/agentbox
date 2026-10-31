import argparse
import json
import subprocess
import time
import unittest
from unittest import mock

from helpers import AgentboxTestCase, FakeSbx, LocalVM, ab

REAL_SLEEP = time.sleep          # ab.time is the real time module, so patching sleep patches ours too
PROMPT = "SECRET-PROMPT-TEXT summarise the repo"
JOB = "20260101T000000Z-abcdef"


def probe(**kw):
    base = {"dir": "/home/agent/.agentbox/jobs/" + JOB, "missing": False, "files": {}, "cmdline": None}
    return {**base, **kw}


class ClassifyTests(unittest.TestCase):
    wrapper = f"/home/agent/.agentbox/jobs/{JOB}/wrap.sh"

    def test_running(self):
        p = probe(files={"pid": "42"}, cmdline=f"sh {self.wrapper} /x /ws claude -p ")
        self.assertEqual(ab.classify_job(p), "running")

    def test_finished_wins(self):
        self.assertEqual(ab.classify_job(probe(files={"pid": "42", "exit_code": "0"})), "finished")

    def test_pid_reused_by_another_process_is_lost(self):
        p = probe(files={"pid": "42"}, cmdline="/usr/bin/python3 server.py ")
        self.assertEqual(ab.classify_job(p), "lost")
        # a different job's wrapper must not count either
        other = self.wrapper.replace("abcdef", "000000")
        self.assertEqual(ab.classify_job(probe(files={"pid": "42"}, cmdline=f"sh {other} ")), "lost")

    def test_lost_cases(self):
        self.assertEqual(ab.classify_job(probe(files={"pid": "42"})), "lost")       # process gone
        self.assertEqual(ab.classify_job(probe(files={})), "lost")                  # never got a pid
        self.assertEqual(ab.classify_job(probe(missing=True)), "lost")              # VM reset
        self.assertEqual(ab.classify_job(probe(files={"pid": "4x"}, cmdline=f"sh {self.wrapper}")), "lost")

    def test_summary_validates_values(self):
        s = ab.job_summary(JOB, "finished", {"exit_code": "3", "session_id": "bad id!", "started_at": "2026-01-01T00:00:00Z",
                                             "finished_at": "yesterday"})
        self.assertEqual((s["exit_code"], s["session_id"], s["started_at"], s["finished_at"]),
                         (3, None, "2026-01-01T00:00:00Z", None))
        self.assertIsNone(ab.job_summary(JOB, "running", {"exit_code": "3"})["exit_code"])

    def test_probe_looks_at_the_process_before_the_files(self):
        # A job that finishes mid-probe must read as finished, never lost: exit_code is written
        # before the process exits, so the files have to be read AFTER the process check.
        script = ab.probe_script(JOB)
        self.assertLess(script.index("/proc/$p/cmdline"), script.index("for f in pid"))
        self.assertLess(script.index("for f in pid"), script.index("exit_code"))

    def test_job_ids_are_validated(self):
        with self.assertRaises(SystemExit):
            ab.probe_script("x; rm -rf ~")


class RetryTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.sleeps = []
        p = mock.patch.object(ab.time, "sleep", self.sleeps.append)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def res(rc, err=""):
        return subprocess.CompletedProcess([], rc, "", err)

    def test_transient_detection(self):
        for msg in ("error: inspect exec: context deadline exceeded", "dial: Connection refused",
                    "read: connection reset by peer"):
            self.assertTrue(ab.is_transient(self.res(1, msg)), msg)
        self.assertFalse(ab.is_transient(self.res(1, "no such sandbox")))
        self.assertFalse(ab.is_transient(self.res(0, "context deadline exceeded")))   # success is success

    def test_backoff_is_exponential_and_capped(self):
        self.assertEqual([ab.backoff_delay(i) for i in range(6)], [1, 2, 4, 8, 15, 15])

    def test_retries_transient_then_succeeds(self):
        seq = iter([self.res(1, "context deadline exceeded"), self.res(1, "connection reset"), self.res(0)])
        r = ab.retry_transient(lambda: next(seq))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.sleeps, [1, 2])

    def test_permanent_error_is_not_retried(self):
        calls = []
        r = ab.retry_transient(lambda: calls.append(1) or self.res(1, "no such sandbox"))
        self.assertEqual((len(calls), r.returncode, self.sleeps), (1, 1, []))

    def test_bounded(self):
        calls = []
        r = ab.retry_transient(lambda: calls.append(1) or self.res(1, "context deadline exceeded"), attempts=3)
        self.assertEqual((len(calls), r.returncode, len(self.sleeps)), (3, 1, 2))

    def prof(self):
        cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        return cfg.profile("p")

    def test_create_does_not_blindly_recreate_if_sandbox_exists(self):
        creates = []

        def fake_sbx(cfg, *args, check=True, capture=False, stdin=None):
            creates.append(args)
            return self.res(1, "error: create: context deadline exceeded")
        with mock.patch.object(ab, "sbx", fake_sbx), mock.patch.object(ab, "sandbox_exists", lambda p, retry=False: True):
            ab.create_sandbox_retrying(self.prof(), ["create", "--name", "ab-p"])
        self.assertEqual(len(creates), 1)

    def test_create_retries_when_sandbox_really_absent_and_dies_on_permanent(self):
        outcomes = iter([self.res(1, "connection refused"), self.res(0)])
        calls = []

        def fake_sbx(cfg, *args, **kw):
            calls.append(args)
            return next(outcomes)
        with mock.patch.object(ab, "sbx", fake_sbx), mock.patch.object(ab, "sandbox_exists", lambda p, retry=False: False):
            ab.create_sandbox_retrying(self.prof(), ["create"])
            self.assertEqual(len(calls), 2)
            with mock.patch.object(ab, "sbx", lambda *a, **k: self.res(1, "bad kit")):
                with self.assertRaises(SystemExit):
                    ab.create_sandbox_retrying(self.prof(), ["create"])


class StreamTests(unittest.TestCase):
    INIT = '{"type":"system","subtype":"init","session_id":"sess-12345678","model":"opus"}'
    ASSIST = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "line one\n  line two"}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls -l"}}]}})

    def test_session_id(self):
        self.assertEqual(ab.session_id_from_stream("garbage\n" + self.INIT + "\n"), "sess-12345678")
        self.assertIsNone(ab.session_id_from_stream('{"type":"assistant"}\nnot json\n'))
        self.assertIsNone(ab.session_id_from_stream('{"session_id":"bad id!"}'))

    def test_render(self):
        self.assertEqual(ab.render_stream_line(self.INIT), "[init] session sess-12345678 model opus")
        lines = ab.render_stream_line(self.ASSIST).split("\n")
        self.assertEqual(lines[0], "assistant: line one line two")
        self.assertTrue(lines[1].startswith("tool: Bash {") and "ls -l" in lines[1])
        self.assertEqual(ab.render_stream_line('{"type":"result","subtype":"success","result":"all done"}'),
                         "[result] success: all done")
        self.assertIsNone(ab.render_stream_line('{"type":"user","message":{}}'))
        self.assertEqual(ab.render_stream_line("oops"), "? oops")
        self.assertIsNone(ab.render_stream_line("  "))

    def test_long_text_is_cut_to_one_line(self):
        ev = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x" * 500}]}})
        self.assertLessEqual(len(ab.render_stream_line(ev)), 200 + len("assistant: "))


class SessionIdTests(unittest.TestCase):
    BAD = ["--settings", "-p", "-resume-12345", "--dangerously-skip-permissions", "-" * 8, "abc", "a" * 81,
           "ok-id-12 3", "ok;id-1234", "ok\nid-1234", ""]

    def test_regex_rejects_option_lookalikes_and_junk(self):
        for sid in self.BAD:
            self.assertFalse(ab.SESSION_ID_RE.match(sid), repr(sid))
        for sid in ("sess-12345678", "0b1c2d3e-4f50-6789-abcd-ef0123456789", "A1_b2-c3d4"):
            self.assertTrue(ab.SESSION_ID_RE.match(sid), sid)

    def test_every_entry_point_applies_it(self):
        for sid in self.BAD:
            self.assertIsNone(ab.session_id_from_stream(json.dumps({"type": "system", "session_id": sid})), repr(sid))
            self.assertIsNone(ab.job_summary(JOB, "finished", {"session_id": sid})["session_id"], repr(sid))


class JobCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n[profiles.sh]\nagent = "shell"\nworkspace = "{self.ws}"\n')
        self.prof = ab.Config().profile("p")
        self.vm = LocalVM(self)
        self.keepalive = []
        p = mock.patch.object(ab, "start_keepalive", lambda prof: self.keepalive.append(prof.name))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ab, "ensure_sandbox", lambda prof, retry=False: None)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ab.time, "sleep", lambda s: REAL_SLEEP(min(s, 0.05)))
        p.start()
        self.addCleanup(p.stop)

    def set_pid(self, job_id, text):
        """Overwrite a job's pid file; the real pid is put back before cleanup so leftovers get stopped."""
        pidfile = self.vm.jobdir(job_id) / "pid"
        original = pidfile.read_text()
        pidfile.write_text(text)
        self.addCleanup(pidfile.write_text, original)

    def args(self, **kw):
        base = dict(prompt=PROMPT, prompt_file=None, detach=True, agent_args=[], job=None, timeout=None,
                    follow=False, raw=False, keepalive=False)
        return argparse.Namespace(**{**base, **kw})

    def start(self, **kw):
        with mock.patch.object(ab, "print", create=True):
            self.assertEqual(ab.cmd_run(self.prof, self.args(**kw)), 0)
        return ab.job_record(self.prof)["id"]

    def finish(self, job_id, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if (self.vm.jobdir(job_id) / "exit_code").exists():
                return
            REAL_SLEEP(0.05)
        self.fail("job did not finish")


class RunTests(JobCase):
    def test_resume_passes_the_id_as_one_equals_argument_and_refuses_evil_ids(self):
        job = self.start()
        self.finish(job)
        runs = []
        orig = type(self.vm).__call__
        with mock.patch.object(type(self.vm), "__call__",
                               lambda s, cfg, *a, **k: (runs.append(a), subprocess.CompletedProcess(a, 0, "", ""))[1]
                               if a[0] == "run" else orig(s, cfg, *a, **k)):
            self.assertEqual(ab.cmd_resume(self.prof, self.args()), 0)
            self.assertEqual(runs[0][-2:], ("--", "--resume=sess-12345678"))
            for evil in ("--settings", "-p"):                      # an agent-written session_id file / host record
                (self.vm.jobdir(job) / "session_id").write_text(evil)
                rec = ab.job_record(self.prof)
                rec["session_id"] = evil
                ab.save_job_record(self.prof, rec)
                runs.clear()
                with self.assertRaises(SystemExit):
                    ab.cmd_resume(self.prof, self.args())
                self.assertEqual(runs, [], evil)

    def test_prompt_never_on_any_argv_and_file_is_private(self):
        job = self.start()
        for call in self.vm.calls:
            self.assertNotIn("SECRET-PROMPT-TEXT", " ".join(map(str, call)))
        self.assertIn(PROMPT, self.vm.stdins)                       # it went in on stdin
        self.finish(job)
        d = self.vm.jobdir(job)
        self.assertEqual((d / "prompt.md").read_text(), PROMPT)
        self.assertEqual(oct((d / "prompt.md").stat().st_mode & 0o777), "0o600")
        self.assertEqual(self.keepalive, [])                       # the keep-alive is opt-in

    def test_keepalive_only_when_asked(self):
        self.finish(self.start())
        self.assertEqual(self.keepalive, [])
        self.finish(self.start(keepalive=True))                    # --keepalive
        self.assertEqual(self.keepalive, ["p"])
        (self.cfg_dir / "agentbox.toml").write_text((self.cfg_dir / "agentbox.toml").read_text()
                                                    .replace('[profiles.p]\n', '[profiles.p]\nkeepalive = true\n', 1))
        self.prof = ab.Config().profile("p")                       # keepalive = true in the profile
        self.finish(self.start())
        self.assertEqual(self.keepalive, ["p", "p"])

    def test_job_files_and_session_id(self):
        job = self.start()
        self.finish(job)
        d = self.vm.jobdir(job)
        for name in ("pid", "started_at", "finished_at", "exit_code", "output.jsonl", "stderr.log"):
            self.assertTrue((d / name).exists(), name)
        self.assertEqual((d / "exit_code").read_text().strip(), "0")
        info = ab.job_info(self.prof)
        self.assertEqual((info["state"], info["exit_code"], info["session_id"]), ("finished", 0, "sess-12345678"))
        self.assertEqual((d / "session_id").read_text().strip(), "sess-12345678")     # cached in the VM ...
        rec = ab.job_record(self.prof)
        self.assertEqual((rec["session_id"], rec["exit_code"]), ("sess-12345678", 0))  # ... and in host state

    def test_running_then_finished_exit_code_passthrough(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        self.assertEqual(ab.job_info(self.prof)["state"], "running")
        self.set_pid(job, "999999")                                  # process "vanished" -> lost
        self.assertEqual(ab.job_info(self.prof)["state"], "lost")

    def test_exit_status_is_the_jobs(self):
        self.vm.env["FAKE_CLAUDE_EXIT"] = "7"
        job = self.start()
        self.finish(job)
        self.assertEqual(ab.cmd_wait(self.prof, self.args()), 7)

    def test_wait_timeout_and_lost(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        self.assertEqual(ab.cmd_wait(self.prof, self.args(timeout=0.2)), ab.EXIT_TIMEOUT)
        self.set_pid(job, "999999")
        self.assertEqual(ab.cmd_wait(self.prof, self.args()), ab.EXIT_LOST)

    def test_running_job_blocks_a_second_run(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.start()
        with self.assertRaises(SystemExit):
            ab.cmd_run(self.prof, self.args())

    def test_unsupported_agent_and_empty_prompt(self):
        sh = ab.Config().profile("sh")
        for prof, kw in ((sh, {}), (self.prof, {"prompt": "  "})):
            with self.assertRaises(SystemExit) as cm:
                ab.cmd_run(prof, self.args(**kw))
            self.assertEqual(cm.exception.code, 2)

    def test_agent_args_are_quoted_not_the_prompt(self):
        job = self.start(agent_args=["--model", "a b"])
        script = [c[-1] for c in self.vm.calls if c[0] == "exec" and "wrap.sh" in c[-1] and "nohup" in c[-1]][0]
        self.assertIn("--model 'a b'", script)
        self.finish(job)


class LogsTests(JobCase):
    def run_logs(self, **kw):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ab.cmd_logs(self.prof, self.args(**kw))
        return code, out.getvalue()

    def test_rendered_and_raw(self):
        self.finish(self.start())
        code, out = self.run_logs()
        self.assertEqual(code, 0)
        self.assertIn("[init] session sess-12345678 model m", out)
        self.assertIn("assistant: hello there", out)
        self.assertIn("tool: Bash", out)
        self.assertIn("[result] success: all done", out)
        _, raw = self.run_logs(raw=True)
        self.assertEqual(len(raw.splitlines()), 3)
        self.assertTrue(all(json.loads(l) for l in raw.splitlines()))

    def test_follow_ends_with_the_job(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "1"
        self.start()
        code, out = self.run_logs(follow=True, wait_status=True)
        self.assertEqual(code, 0)
        self.assertIn("[result] success", out)


class StatusDownTests(JobCase):
    def test_status_json_job_field(self):
        code, out, _ = self.run_main("status", "--json")
        prof = {r["name"]: r for r in json.loads(out)["profiles"]}["p"]
        self.assertEqual(prof["job"], ab.NO_JOB)
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        _, out, _ = self.run_main("status", "--json")
        j = {r["name"]: r for r in json.loads(out)["profiles"]}["p"]["job"]
        self.assertEqual((j["id"], j["state"], j["exit_code"]), (job, "running", None))
        self.assertEqual(set(j), set(ab.NO_JOB))

    def test_status_json_finished(self):
        job = self.start()
        self.finish(job)
        _, out, _ = self.run_main("status", "--json")
        j = {r["name"]: r for r in json.loads(out)["profiles"]}["p"]["job"]
        self.assertEqual((j["state"], j["exit_code"], j["session_id"]), ("finished", 0, "sess-12345678"))
        self.assertTrue(j["started_at"] and j["finished_at"])

    def test_down_refuses_while_a_job_runs_unless_forced(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.start()
        code, _, err = self.run_main("down", "p")
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertFalse(any(c[0] == "rm" for c in self.vm.calls))
        code, _, err = self.run_main("down", "p", "--force")
        self.assertEqual(code, 0)
        self.assertIn("killing running job", err)
        self.assertTrue(any(c[0] == "rm" for c in self.vm.calls))


class KeepAliveTests(JobCase):
    def run_sh(self, script):
        from helpers import REAL_POPEN, REAL_RUN
        with mock.patch.object(subprocess, "Popen", REAL_POPEN):
            return REAL_RUN(["sh", "-c", script], env=self.vm.env, capture_output=True, text=True, timeout=20)

    def test_hold_session_ends_when_job_finished_or_process_gone(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        self.assertEqual(self.run_sh(f"( {ab.hold_script(job)} ) & p=$!; sleep 0.5; kill -0 $p && echo held; kill $p").stdout.strip(), "held")
        self.set_pid(job, str(self.spawn_sleep().pid))               # alive, but not our wrapper
        self.assertEqual(self.run_sh(ab.hold_script(job)).returncode, 0)    # returns at once
        (self.vm.jobdir(job) / "exit_code").write_text("0")
        self.assertEqual(self.run_sh(ab.hold_script(job)).returncode, 0)

    def test_keepalive_loop_restarts_a_dropped_session_then_stops_with_the_job(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        procs = []

        class Held:
            def __init__(self, cmd, **kw):
                procs.append(cmd)
                self.cmd = cmd

            def wait(self):
                if len(procs) == 2:                         # second drop: the job has ended by then
                    (self_outer.vm.jobdir(job) / "exit_code").write_text("0")

            def terminate(self):
                pass
        self_outer = self
        with mock.patch.object(subprocess, "Popen", Held):
            self.assertEqual(ab.cmd_keepalive(self.prof), 0)
        self.assertEqual(len(procs), 2)
        self.assertEqual(procs[0][1:4], ["exec", "ab-p", "sh"])
        self.assertNotIn("SECRET-PROMPT-TEXT", " ".join(procs[0]))


class KillSafetyTests(JobCase):
    """A job's pid file is agent-writable. Writing "1" into one and then SIGTERMing it once took the
    whole VM down (PID 1 is init), so the one place that signals a job pid must validate it."""

    def setUp(self):
        super().setUp()
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.job = self.start()
        self.real_pid = (self.vm.jobdir(self.job) / "pid").read_text().strip()
        self.log = self.tmp / "kills.log"

    def run_kill(self, pid_text=None, own_pid=False):
        """Run kill_script with `kill` shadowed by a function that only records the call: nothing
        is ever signalled in this test, whatever the validation decides."""
        from helpers import REAL_POPEN, REAL_RUN
        self.log.write_text("")
        d = f'd="$HOME/.agentbox/jobs/{self.job}"; '
        pre = d + ('echo $$ > "$d/pid"; ' if own_pid else f'printf %s "{pid_text}" > "$d/pid"; ')
        script = pre + f'kill() {{ echo "$*" >> "{self.log}"; }}; ' + ab.kill_script(self.job)
        with mock.patch.object(subprocess, "Popen", REAL_POPEN):
            REAL_RUN(["sh", "-c", script], env=self.vm.env, capture_output=True, timeout=20)
        (self.vm.jobdir(self.job) / "pid").write_text(self.real_pid)     # leave the real pid for cleanup
        return self.log.read_text()

    def test_refuses_pid_0_1_negative_garbage_empty_and_ourselves(self):
        import os
        for text in ("0", "1", "-1", "-2", "", "abc", "12 34", "1; echo x", "00", "01"):
            self.assertEqual(self.run_kill(text), "", repr(text))
        self.assertEqual(self.run_kill(str(os.getpid())), "", "parent of the shell")
        self.assertEqual(self.run_kill(own_pid=True), "", "the shell itself")

    def test_refuses_a_live_pid_that_is_not_this_jobs_wrapper(self):
        other = self.spawn_sleep()
        self.assertEqual(self.run_kill(str(other.pid)), "")
        self.assertIsNone(other.poll())                                  # still alive

    def test_signals_the_real_wrapper_group(self):
        self.assertEqual(self.run_kill(self.real_pid).split(), ["-TERM", "--", f"-{self.real_pid}"])

    def test_force_down_uses_the_validated_killer(self):
        code, _, err = self.run_main("down", "p", "--force")
        self.assertEqual(code, 0)
        kills = [c[-1] for c in self.vm.calls if c[0] == "exec" and "kill -TERM" in c[-1]]
        self.assertEqual(kills, [ab.kill_script(self.job)])
        self.assertIn("proc/$p/cmdline", kills[0])

    def test_kill_guard_in_the_test_harness_itself(self):
        import os
        for pid in (0, 1, -1, os.getpid(), os.getppid()):
            with self.assertRaises(AssertionError):
                os.kill(pid, 15)
        with self.assertRaises(AssertionError):
            os.killpg(1, 15)
        other = self.spawn_sleep()
        os.kill(other.pid, 15)                                           # registered: allowed
        other.wait(timeout=5)


class HostPidTests(AgentboxTestCase):
    """stop_keepalive / stop_bridge only ever signal our own `agentbox <word> <profile>` process."""

    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        self.prof = ab.Config().profile("p")
        self.prof.state.mkdir(parents=True)
        self.ps_calls = []

    def fake_ps(self, command):
        def run(cmd, **kw):
            self.ps_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, command + "\n", "")
        return mock.patch.object(subprocess, "run", run)

    def pidfile(self, word, text):
        pf = self.prof.state / f"{word}.pid"
        pf.write_text(str(text))
        return pf

    def test_unsafe_pids_are_dropped_without_asking_ps_or_signalling(self):
        import os
        with self.fake_ps("python3 /x/agentbox.py keepalive p"):          # even if ps "matches"
            for text in ("0", "1", "-1", os.getpid(), os.getppid(), "junk"):
                pf = self.pidfile("keepalive", text)
                ab.stop_keepalive(self.prof)                              # os.kill is guarded: would raise
                self.assertFalse(pf.exists(), text)
        self.assertEqual(self.ps_calls, [])

    def test_unrelated_process_is_left_alone_and_pidfile_removed(self):
        other = self.spawn_sleep()
        for word, stop in (("keepalive", ab.stop_keepalive), ("bridge", ab.stop_bridge)):
            for command in ("vim notes.txt", f"python3 /x/agentbox.py {word} other-profile",
                            "python3 /x/agentbox.py status p"):
                pf = self.pidfile(word, other.pid)
                with self.fake_ps(command):
                    stop(self.prof)
                self.assertFalse(pf.exists(), command)
                self.assertIsNone(other.poll(), command)

    def test_our_own_process_is_signalled(self):
        for word, stop in (("keepalive", ab.stop_keepalive), ("bridge", ab.stop_bridge)):
            proc = self.spawn_sleep()
            pf = self.pidfile(word, proc.pid)
            with self.fake_ps(f"/usr/bin/python3 /home/u/agentbox.py {word} p"):
                stop(self.prof)
            proc.wait(timeout=5)
            self.assertFalse(pf.exists())


if __name__ == "__main__":
    unittest.main()
