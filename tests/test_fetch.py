"""`agentbox fetch`: sbx and the keep-awake child are stubbed; the git parts run on real temporary repos."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import REAL_RUN, AgentboxTestCase, ab, real_sh

REAL_SLEEP, REAL_MONOTONIC = time.sleep, time.monotonic


def from_agentbox() -> bool:
    """True when the caller of the patched time function is agentbox itself. `ab.time` is the real `time`
    module, so the fakes are process-wide: without this check the stdlib's own polling (subprocess waiting
    for a slow real git) hit the fake sleep, which counted it as a poll or raised the test's KeyboardInterrupt."""
    return sys._getframe(2).f_globals.get("__name__") == ab.__name__


GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}


class FakeChild:
    def __init__(self, args):
        self.args, self.rc, self.terminated, self.killed = args, None, False, False

    def poll(self):
        return self.rc

    def terminate(self):
        self.terminated, self.rc = True, -15

    def kill(self):
        self.killed, self.rc = True, -9

    def wait(self, timeout=None):
        return self.rc


@unittest.skipUnless(shutil.which("git"), "needs git")
class FetchCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.host = self.ws
        self.git(self.host, "init", "-q", "-b", "main")
        self.commit(self.host, "base")
        self.box = self.tmp / "box"                         # the agent's clone inside the "VM"
        self.git(self.tmp, "clone", "-q", str(self.host), "box")
        self.commit(self.box, "agent one")
        self.cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.host}"\nmode = "clone"\n')
        self.vm_state, self.appear_after, self.sleeps, self.children, self.clock = "running", 0, 0, [], 0.0
        self.interrupt = False
        for target, name, fn in ((subprocess, "run", self.fake_run), (subprocess, "Popen", self.fake_popen),
                                 (ab, "sandbox_state", lambda prof, states=None: self.vm_state),
                                 (ab.time, "sleep", self.fake_sleep), (ab.time, "monotonic", self.fake_monotonic)):
            p = mock.patch.object(target, name, fn)
            p.start()
            self.addCleanup(p.stop)

    # --- git helpers (always the real git, on temp repos)
    def git(self, repo, *args):
        with real_sh():
            r = REAL_RUN(["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def commit(self, repo, subject):
        self.git(repo, "commit", "-q", "--allow-empty", "-m", subject)
        return self.git(repo, "rev-parse", "HEAD")

    def fake_run(self, cmd, *a, **kw):
        if cmd[0] != "git":
            raise AssertionError(f"unexpected subprocess.run: {cmd!r}")
        if cmd[:3] == ["git", "-C", str(self.host)] and cmd[3] == "remote" and len(cmd) == 4 and self.appear_after == 0:
            self.add_remote()
        with real_sh():
            return REAL_RUN(cmd, *a, **kw)

    def add_remote(self):
        if "sandbox-ab-p" not in self.git(self.host, "remote").split():
            self.git(self.host, "remote", "add", "sandbox-ab-p", str(self.box))

    def fake_popen(self, args, **kw):
        child = FakeChild(args)
        self.children.append(child)
        return child

    def fake_monotonic(self):
        return self.clock if from_agentbox() else REAL_MONOTONIC()

    def fake_sleep(self, s):
        if not from_agentbox():
            return REAL_SLEEP(s)
        self.sleeps += 1
        self.clock += s
        if self.interrupt:
            raise KeyboardInterrupt
        if self.appear_after and self.sleeps >= self.appear_after:
            self.add_remote()

    def fetch(self, *extra):
        return self.run_main("fetch", "p", *extra)


class FetchTests(FetchCase):
    def test_fetches_into_review_branch_and_reports_without_merging(self):
        head = self.git(self.host, "rev-parse", "HEAD")
        code, out, err = self.fetch()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.git(self.host, "rev-parse", "review/p"), self.git(self.box, "rev-parse", "HEAD"))
        self.assertEqual(self.git(self.host, "rev-parse", "HEAD"), head, "must never merge")
        self.assertIn("agent one", out)
        self.assertIn("git diff HEAD...review/p", out)
        self.assertIn("git merge --ff-only review/p", out)

    def test_stopped_vm_without_start_refuses_and_runs_nothing(self):
        self.vm_state = "stopped"
        code, out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("--start", err)
        self.assertIn("relaunches the agent", err)
        self.assertEqual(self.children, [])

    def test_start_boots_through_the_keep_awake_session(self):
        self.vm_state = "stopped"
        code, out, err = self.fetch("--start")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.children[0].args, ["sbx-must-not-run", "exec", "ab-p", "sleep", "300"])

    def test_missing_sandbox_exits_1_and_points_at_kept_refs(self):
        self.vm_state = "missing"
        code, out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertNotIn("refs/sandboxes", err)
        self.git(self.host, "update-ref", "refs/sandboxes/ab-p/main", self.git(self.host, "rev-parse", "HEAD"))
        code, out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("git branch LOCAL refs/sandboxes/ab-p/main", err)
        self.assertEqual(self.children, [])

    def test_direct_mode_refuses(self):
        self.write_config(f'[profiles.p]\nworkspace = "{self.host}"\nmode = "direct"\n')
        code, out, err = self.fetch()
        self.assertEqual(code, 2)
        self.assertIn("agentbox audit p", err)
        self.assertEqual(self.children, [])


class KeepAwakeTests(FetchCase):
    def test_child_is_terminated_on_success(self):
        self.assertEqual(self.fetch()[0], 0)
        self.assertTrue(self.children[0].terminated)

    def test_child_is_terminated_on_error(self):
        self.fetch()
        self.commit(self.host, "diverge")
        self.git(self.box, "reset", "-q", "--hard", "HEAD~1")
        self.commit(self.box, "different")
        self.children.clear()
        code, out, err = self.fetch()                       # review/p is no ancestor of the new main
        self.assertEqual(code, 1)
        self.assertTrue(self.children[0].terminated)

    def test_child_is_terminated_on_keyboard_interrupt(self):
        self.appear_after, self.interrupt = 5, True
        code, out, err = self.fetch()
        self.assertEqual(code, 130)
        self.assertTrue(self.children[0].terminated)

    def test_stubborn_child_is_killed(self):
        class Stubborn(FakeChild):
            def terminate(self):
                pass

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired("sbx", timeout)
                return self.rc
        self.fake_popen = lambda args, **kw: self.children.append(Stubborn(args)) or self.children[-1]
        with mock.patch.object(subprocess, "Popen", self.fake_popen):
            self.assertEqual(self.fetch()[0], 0)
        self.assertTrue(self.children[0].killed)

    def test_stdlib_sleep_is_not_counted_or_interrupted(self):
        # A real process that outlives subprocess's first poll makes the stdlib call time.sleep itself;
        # that must reach the real sleep, not the fake (it used to add a poll or raise the interrupt).
        self.interrupt = True
        with real_sh():
            REAL_RUN(["sleep", "0.2"], timeout=10)
        self.assertEqual(self.sleeps, 0)
        self.assertEqual(self.clock, 0.0)

    def test_remote_appears_after_n_polls(self):
        self.appear_after = 3
        code, out, err = self.fetch()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sleeps, 3)

    def test_timeout_when_the_remote_never_appears(self):
        self.appear_after = 10 ** 9
        code, out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("did not appear within 60s", err)
        self.assertTrue(self.children[0].terminated)
        self.assertGreaterEqual(self.clock, 60)

    def test_dead_keep_awake_session_stops_the_wait(self):
        self.appear_after = 10 ** 9
        with mock.patch.object(subprocess, "Popen", lambda args, **kw: self.children.append(FakeChild(args))
                               or setattr(self.children[-1], "rc", 1) or self.children[-1]):
            code, out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("keep-awake session", err)
        self.assertEqual(self.sleeps, 0)


class ValidationTests(FetchCase):
    BAD = ["-x", "a..b", "a b", "a@{b", "@{-1}", "x:y", "a.lock", "/a", "a/", "a//b", "a/.b", "a~1", "a^", "a*", ""]

    def test_bad_names_are_refused_before_anything_runs(self):
        for flag in ("--ref", "--branch"):
            for bad in self.BAD:
                with self.subTest(flag=flag, bad=bad):
                    code, out, err = self.fetch(f"{flag}={bad}")
                    self.assertEqual(code, 2, err)
        self.assertEqual(self.children, [])

    def test_branch_checked_out_in_the_host_refuses(self):
        code, out, err = self.fetch("--branch", "main")
        self.assertEqual(code, 2)
        self.assertIn("checked out", err)
        self.assertEqual(self.children, [])

    def test_branch_checked_out_in_another_worktree_refuses(self):
        self.git(self.host, "worktree", "add", "-q", "-b", "review/p", str(self.tmp / "wt"))
        self.assertEqual(self.fetch()[0], 2)

    def test_non_fast_forward_needs_force(self):
        self.assertEqual(self.fetch()[0], 0)
        old = self.git(self.host, "rev-parse", "review/p")
        self.git(self.box, "reset", "-q", "--hard", "HEAD~1")
        new = self.commit(self.box, "rewritten")
        code, out, err = self.fetch()
        self.assertEqual(code, 1)
        self.assertIn("--force", err)
        self.assertEqual(self.git(self.host, "rev-parse", "review/p"), old)
        code, out, err = self.fetch("--force", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.git(self.host, "rev-parse", "review/p"), new)
        self.assertEqual(json.loads(out)["previous"], old)

    def test_fast_forward_of_the_review_branch_is_fine(self):
        self.assertEqual(self.fetch()[0], 0)
        new = self.commit(self.box, "agent two")
        self.assertEqual(self.fetch()[0], 0)
        self.assertEqual(self.git(self.host, "rev-parse", "review/p"), new)


class ReportTests(FetchCase):
    def test_output_is_safe_text(self):
        self.commit(self.box, "evil \x1b]52;c;ZXZpbA==\x07 subject")
        (self.box / "we\x1bird.txt").write_text("x\n")
        self.git(self.box, "add", "-A")
        self.git(self.box, "commit", "-q", "-m", "files")
        code, out, err = self.fetch()
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out + err)
        self.assertNotIn("\x07", out)
        self.assertIn("\\x1b]52", out)

    def test_json_shape(self):
        (self.box / "f.txt").write_text("a\nb\n")
        self.git(self.box, "add", "-A")
        self.git(self.box, "commit", "-q", "-m", "add f")
        code, out, err = self.fetch("--json")
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        tip = self.git(self.box, "rev-parse", "HEAD")
        self.assertEqual(d, {"profile": "p", "sandbox": "ab-p", "remote": "sandbox-ab-p", "ref": "main",
                             "branch": "review/p", "commit": tip, "previous": None, "fast_forward_from_head": True,
                             "commits": [{"hash": tip, "subject": "add f"},
                                         {"hash": self.git(self.box, "rev-parse", "HEAD~1"), "subject": "agent one"}],
                             "commits_total": 2, "files_changed": 1, "insertions": 2, "deletions": 0,
                             "job_running": False})

    def test_log_is_capped(self):
        for i in range(54):
            self.commit(self.box, f"c{i}")
        code, out, err = self.fetch()
        self.assertEqual(code, 0, err)
        self.assertIn("... 5 more", out)
        self.assertIn("55 commit(s)", out)

    def test_file_list_is_capped(self):
        for i in range(35):
            (self.box / f"f{i:02}.txt").write_text("x\n")
        self.git(self.box, "add", "-A")
        self.git(self.box, "commit", "-q", "-m", "many")
        code, out, err = self.fetch()
        self.assertIn("f29.txt", out)
        self.assertNotIn("f30.txt", out)
        self.assertIn("... 5 more files", out)
        self.assertIn("35 files changed", out)

    def test_diverged_head_has_no_fast_forward_hint(self):
        self.commit(self.host, "host moved on")
        code, out, err = self.fetch("--json")
        self.assertFalse(json.loads(out)["fast_forward_from_head"])
        code, out, err = self.fetch()
        self.assertIn("fast-forward is not possible", out)
        self.assertNotIn("merge --ff-only", out)

    def test_running_job_is_flagged(self):
        job = {"id": "20260101T120000Z-abcdef", "state": "running"}
        with mock.patch.object(ab, "job_info", lambda prof, *a, **k: job), \
                mock.patch.object(ab, "job_progress", lambda *a, **k: (None, "")):
            code, out, err = self.fetch("--json")
            self.assertTrue(json.loads(out)["job_running"])
            self.assertIn("still running", err)


class HintTests(FetchCase):
    def test_follow_up_hints_name_fetch_for_clone_profiles(self):
        prof = self.cfg.profile("p")
        j = {"state": "finished", "exit_code": 0, "id": "20260101T120000Z-abcdef"}
        with mock.patch.object(ab, "resolve_job", lambda *a: j["id"]), mock.patch.object(ab, "job_info", lambda *a, **k: j), \
                mock.patch.object(ab, "cache_final_progress"):
            code, out, err = self.run_main("wait", "p")
        self.assertIn("agentbox fetch p", err)
        self.write_config(f'[profiles.p]\nworkspace = "{self.host}"\n')
        with mock.patch.object(ab, "resolve_job", lambda *a: j["id"]), mock.patch.object(ab, "job_info", lambda *a, **k: j), \
                mock.patch.object(ab, "cache_final_progress"):
            code, out, err = self.run_main("wait", "p")
        self.assertNotIn("agentbox fetch", err)
