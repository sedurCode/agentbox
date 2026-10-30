"""Nothing the job code does may boot a stopped sandbox (`sbx exec` starts one)."""
import json
import subprocess
import unittest
from unittest import mock

import test_jobs
from helpers import AgentboxTestCase, FakeSbx, ab


class SandboxStateTests(AgentboxTestCase):
    def state(self, stdout=None, rc=0):
        cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        r = subprocess.CompletedProcess([], rc, stdout, "")
        with mock.patch.object(ab, "sbx", lambda *a, **k: r):
            return ab.sandbox_state(cfg.profile("p"))

    def test_states(self):
        row = lambda **kw: json.dumps([{"name": "other", "status": "stopped"}, {"name": "ab-p", **kw}])
        self.assertEqual(self.state(row(status="running", id="x", agent="claude")), "running")
        self.assertEqual(self.state(row(status="stopped")), "stopped")
        self.assertEqual(self.state(json.dumps([{"name": "other", "status": "running"}])), "missing")
        self.assertEqual(self.state("[]"), "missing")

    def test_unknown(self):
        self.assertEqual(self.state("", rc=1), "unknown")                     # call failed
        self.assertEqual(self.state("not json"), "unknown")
        self.assertEqual(self.state('{"name": "ab-p"}'), "unknown")           # not a list
        self.assertEqual(self.state(json.dumps([{"name": "ab-p", "status": "paused"}])), "unknown")

    def test_transient_ls_error_is_retried(self):
        cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        seq = iter([subprocess.CompletedProcess([], 1, "", "context deadline exceeded"),
                    subprocess.CompletedProcess([], 0, '[{"name":"ab-p","status":"running"}]', "")])
        with mock.patch.object(ab, "sbx", lambda *a, **k: next(seq)), mock.patch.object(ab.time, "sleep"):
            self.assertEqual(ab.sandbox_state(cfg.profile("p")), "running")


class StoppedJobTests(test_jobs.JobCase):
    def execs(self):
        return [c for c in self.vm.calls if c[0] == "exec"]

    def test_stopped_vm_means_lost_without_exec_and_is_cached(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        self.vm.vm = "stopped"
        n_exec = len(self.execs())
        j = ab.job_info(self.prof)
        self.assertEqual((j["id"], j["state"], j["vm"]), (job, "lost", "stopped"))
        self.assertFalse(j.get("probe_failed"))
        self.assertEqual(len(self.execs()), n_exec)                # no exec: nothing was booted
        self.assertEqual(ab.job_record(self.prof)["state"], "lost")
        calls = len(self.vm.calls)
        self.vm.vm = "running"                                     # even if it comes back, a lost job is lost
        self.assertEqual(ab.job_info(self.prof)["state"], "lost")
        self.assertEqual(len(self.vm.calls), calls, "cached: no sbx call at all")

    def test_finished_job_is_answered_from_cache_even_with_the_vm_stopped(self):
        job = self.start()
        self.finish(job)
        self.assertEqual(ab.job_info(self.prof)["state"], "finished")
        self.vm.vm = "stopped"
        calls = len(self.vm.calls)
        j = ab.job_info(self.prof)
        self.assertEqual((j["state"], j["exit_code"], j["session_id"]), ("finished", 0, "sess-12345678"))
        self.assertEqual(len(self.vm.calls), calls)

    def test_unknown_state_is_not_cached_and_flags_probe_failed(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.start()
        self.vm.ls_fail = True
        j = ab.job_info(self.prof)
        self.assertEqual((j["state"], j["vm"], j.get("probe_failed")), ("lost", "unknown", True))
        self.assertNotIn("state", ab.job_record(self.prof))        # transient: may still be running
        self.vm.ls_fail = False
        self.assertEqual(ab.job_info(self.prof)["state"], "running")

    def test_public_job_has_vm(self):
        self.assertEqual(set(ab.public_job(ab.job_info(self.prof))), set(ab.NO_JOB))
        self.assertIn("vm", ab.NO_JOB)

    def test_wait_on_a_stopped_vm_is_lost_at_once(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.start()
        self.vm.vm = "stopped"
        with mock.patch.object(ab.time, "sleep", side_effect=AssertionError("no polling expected")):
            self.assertEqual(ab.cmd_wait(self.prof, self.args()), ab.EXIT_LOST)

    def test_wait_keeps_its_doubt_loop_for_unknown_only(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.start()
        self.vm.ls_fail = True
        before = sum(c == ("ls", "--json") for c in self.vm.calls)
        self.assertEqual(ab.cmd_wait(self.prof, self.args()), ab.EXIT_LOST)
        tried = sum(c == ("ls", "--json") for c in self.vm.calls) - before
        self.assertGreaterEqual(tried, 3 * 1)                      # three probes (each may retry) before giving up

    def test_logs_refuses_to_boot_unless_started(self):
        job = self.start()
        self.finish(job)
        self.vm.vm = "stopped"
        n_exec = len(self.execs())
        for follow in (False, True):
            with self.assertRaises(SystemExit) as cm:
                ab.cmd_logs(self.prof, self.args(follow=follow))
            self.assertEqual(cm.exception.code, ab.EXIT_NO_SANDBOX)
        self.assertEqual(len(self.execs()), n_exec)
        self.vm.boot = True                                        # --start: boot it, then read
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(ab.cmd_logs(self.prof, self.args(start=True)), 0)
        self.assertIn("[result] success", out.getvalue())
        self.assertEqual(self.vm.vm, "running")

    def test_logs_missing_sandbox_is_3_and_unknown_is_1(self):
        self.finish(self.start())
        self.vm.exists = False
        with self.assertRaises(SystemExit) as cm:
            ab.cmd_logs(self.prof, self.args())
        self.assertEqual(cm.exception.code, ab.EXIT_NO_SANDBOX)
        self.vm.exists, self.vm.ls_fail = True, True
        with self.assertRaises(SystemExit) as cm:
            ab.cmd_logs(self.prof, self.args())
        self.assertEqual(cm.exception.code, ab.EXIT_FAIL)

    def test_keepalive_exits_without_exec_when_the_vm_is_not_running(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        self.start()
        for state in ("stopped",):
            self.vm.vm = state
            self.assertEqual(ab.cmd_keepalive(self.prof), 0)      # Popen is forbidden in tests: it would fail
        self.vm.vm, self.vm.exists = "running", False             # sandbox removed
        self.assertEqual(ab.cmd_keepalive(self.prof), 0)

    def test_status_never_execs_into_a_stopped_vm(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"
        job = self.start()
        self.vm.vm = "stopped"
        n_exec = len(self.execs())
        code, out, _ = self.run_main("status", "--json")
        j = {r["name"]: r for r in json.loads(out)["profiles"]}["p"]["job"]
        self.assertEqual((j["id"], j["state"], j["vm"]), (job, "lost", "stopped"))
        self.assertEqual(len(self.execs()), n_exec)

    def test_resume_uses_the_recorded_session_without_booting_to_read_it(self):
        job = self.start()
        self.finish(job)
        ab.job_info(self.prof)                                     # records the session id
        self.vm.vm = "stopped"
        runs = []
        orig = self.vm.__class__.__call__
        with mock.patch.object(self.vm.__class__, "__call__",
                               lambda s, cfg, *a, **k: (runs.append(a), subprocess.CompletedProcess(a, 0, "", ""))[1]
                               if a[0] == "run" else orig(s, cfg, *a, **k)):
            self.assertEqual(ab.cmd_resume(self.prof, self.args()), 0)
        self.assertEqual(len(runs), 1)
        self.assertEqual(self.vm.vm, "stopped")                    # reading the session id booted nothing


class StoppedDiagnosticsTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.on]\nworkspace = "{self.ws}"\n')

    def test_doctor_skips_in_sandbox_checks_when_stopped(self):
        fake = FakeSbx(exists=True, vm="stopped", names=("ab-on",)).install(self)
        code, out, _ = self.run_main("doctor", "on")
        self.assertEqual(code, 0)
        self.assertIn("in-sandbox checks skipped (sandbox stopped)", out)
        self.assertFalse([c for c in fake.calls if c[0] == "exec"])

    def test_doctor_json_reports_the_skip(self):
        FakeSbx(exists=True, vm="stopped", names=("ab-on",)).install(self)
        code, out, _ = self.run_main("doctor", "on", "--json")
        skips = [c["message"] for c in json.loads(out)["checks"] if c["status"] == "skip"]
        self.assertEqual(skips, ["skipped (sandbox stopped)"])

    def test_doctor_start_boots_and_checks(self):
        fake = FakeSbx(exists=True, vm="stopped", names=("ab-on",), boot=True,
                       exec_out={"TOKENVAR": (0, "TOKENVAR A_KEY placeholder 3\n"),
                                 "ssh-add": (2, "Could not open a connection to your authentication agent.\n")}).install(self)
        code, out, _ = self.run_main("doctor", "on", "--start")
        self.assertEqual(code, 0)
        self.assertIn("A_KEY: placeholder/unknown", out)
        self.assertTrue([c for c in fake.calls if c[0] == "exec"])

    def test_audit_git_status_does_not_boot_a_stopped_sandbox(self):
        prof = ab.Config().profile("on")
        FakeSbx(exists=True, vm="stopped", names=("ab-on",)).install(self)
        self.assertIsNone(ab.sandbox_git_status(prof, str(self.ws)))   # subprocess.run is forbidden: not reached


if __name__ == "__main__":
    unittest.main()
