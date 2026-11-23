"""The branch a job ends on: recorded by the wrapper, read back by the probe, validated on the host, shown
in progress / status / the dashboard, and used by `fetch` to choose what to fetch."""
import json
import shutil
import unittest
from unittest import mock

from helpers import REAL_RUN, AgentboxTestCase, ab, needs_linux_vm, real_sh
from test_dashboard_interactive import NOW, data, job, row, text_of
from test_fetch import FetchCase
from test_jobs import JobCase

HEAD = "0123456789abcdef0123456789abcdef01234567"
JOB = "20261123T164235Z-dbb67c"


class BranchValidatorTests(unittest.TestCase):
    GOOD = ["main", "opencode-local", "interactive-dashboard", "feature/x", "a/b/c", "v1.2", "x_y", "a" * 100,
            "release/2026.11", "A9"]
    BAD = ["", "-x", "--force", ".x", "../x", "x/../y", "a..b", "x.lock", "a/x.lock", "a.lock/b", "x/", "x.",
           "a//b", "a/.b", "a" * 101, "a" * 300, "a b", " x", "x ", "a\tb", "a\nb", "x\n", "a\x1bb", "a\x00b",
           "a\x7fb", "a‮b", "é", "refs/heads/x", "refs/x", "HEAD", "a@{1}", "@", "a~1", "a^", "a:b", "a*",
           "a?", "a[b", "a\\b", "/x", "_x", None, 5, ["main"]]

    def test_plain_branch_names_pass(self):
        for name in self.GOOD:
            with self.subTest(name=name):
                self.assertEqual(ab.job_branch(name), name)

    def test_hostile_names_are_unknown(self):
        for name in self.BAD:
            with self.subTest(name=name):
                self.assertIsNone(ab.job_branch(name))

    def test_every_accepted_name_passes_check_fetch_ref_and_git(self):
        if not shutil.which("git"):
            self.skipTest("needs git")
        with real_sh(), mock.patch.object(ab.subprocess, "run", REAL_RUN):
            for name in self.GOOD:
                self.assertEqual(ab.check_fetch_ref("--ref", name), name)
                defaults = ab.fetch_defaults("p" * 41, {"id": JOB, "state": "finished", "branch": name})
                self.assertEqual(ab.check_fetch_ref("--branch", defaults["branch"]), defaults["branch"])


class ProbeTests(unittest.TestCase):
    def probe(self, **files):
        out = "D /home/agent/.agentbox/jobs/x\n" + "".join(f"F {k} {v}\n" for k, v in files.items())
        return ab.parse_probe(out)

    def test_branch_and_end_head_are_read_back(self):
        p = self.probe(pid="12", exit_code="0", branch="feature/x", end_head=HEAD)
        self.assertEqual((p["files"]["branch"], p["files"]["end_head"]), ("feature/x", HEAD))
        s = ab.job_summary(JOB, "finished", p["files"])
        self.assertEqual((s["branch"], s["end_head"]), ("feature/x", HEAD))

    def test_hostile_or_missing_values_are_null(self):
        for branch, head in (("-x", HEAD), ("../x", "ABCDEF" + HEAD[6:]), ("a b c", HEAD[:39]), ("", "")):
            s = ab.job_summary(JOB, "finished", self.probe(exit_code="0", branch=branch, end_head=head)["files"])
            self.assertIsNone(s["branch"], branch)
            if head != HEAD:
                self.assertIsNone(s["end_head"], head)
        s = ab.job_summary(JOB, "finished", self.probe(exit_code="0")["files"])     # detached / an older job
        self.assertEqual((s["branch"], s["end_head"]), (None, None))

    def test_running_job_has_no_branch_yet(self):
        s = ab.job_summary(JOB, "running", self.probe(pid="1", branch="feature/x", end_head=HEAD)["files"])
        self.assertEqual((s["branch"], s["end_head"]), (None, None))

    def test_probe_script_reads_the_files(self):
        script = ab.probe_script(JOB)
        self.assertIn("branch end_head", script)
        self.assertIn("head -c 200", script)                 # a 101-char name is not cut down to a valid one

    def test_public_job_has_the_keys(self):
        self.assertEqual({"branch", "end_head"} <= set(ab.public_job(ab.NO_JOB)), True)


class FetchDefaultsTests(unittest.TestCase):
    def test_job_branch(self):
        t = ab.fetch_defaults("p", {"id": JOB, "state": "finished", "branch": "feature/x"})
        self.assertEqual((t["ref"], t["branch"], t["source"]), ("feature/x", "review/p-feature-x", "job"))
        self.assertIn(JOB, t["why"])

    def test_job_on_main_keeps_the_old_review_branch(self):
        t = ab.fetch_defaults("p", {"id": JOB, "state": "finished", "branch": "main"})
        self.assertEqual((t["ref"], t["branch"]), ("main", "review/p"))

    def test_detached_unknown_or_hostile_branch_falls_back_to_main(self):
        for j in ({"id": JOB, "state": "finished", "branch": None}, {"id": JOB, "state": "lost"},
                  {"id": JOB, "state": "finished", "branch": "../evil"},
                  {"id": JOB, "state": "finished", "branch": "-x"}):
            t = ab.fetch_defaults("p", j)
            self.assertEqual((t["ref"], t["branch"], t["source"]), ("main", "review/p", "default"), j)
            self.assertIn("no branch recorded", t["why"])

    def test_running_job_says_so(self):
        t = ab.fetch_defaults("p", {"id": JOB, "state": "running", "branch": None})
        self.assertEqual(t["ref"], "main")
        self.assertIn("still running", t["why"])

    def test_no_job(self):
        for j in (None, {}, dict(ab.NO_JOB)):
            t = ab.fetch_defaults("p", j)
            self.assertEqual((t["ref"], t["branch"]), ("main", "review/p"))
            self.assertIn("no job recorded", t["why"])

    def test_explicit_flags_win(self):
        j = {"id": JOB, "state": "finished", "branch": "feature/x"}
        self.assertEqual(ab.fetch_defaults("p", j, ref="other")["ref"], "other")
        self.assertEqual(ab.fetch_defaults("p", j, ref="other")["branch"], "review/p")     # as before
        self.assertEqual(ab.fetch_defaults("p", j, branch="mine")["branch"], "mine")
        self.assertEqual(ab.fetch_defaults("p", j, branch="mine")["ref"], "feature/x")
        self.assertEqual(ab.fetch_defaults("p", j, ref="a", branch="b")["source"], "flag")


class FetchFollowsTheJobTests(FetchCase):
    def record_job(self, branch):
        ab.save_job_record(self.cfg.profile("p"), {"id": JOB, "state": "finished", "exit_code": 0,
                                                   "branch": branch, "end_head": None})

    def test_plain_fetch_follows_the_jobs_branch(self):
        self.git(self.box, "checkout", "-q", "-b", "feature/x")
        tip = self.commit(self.box, "on the branch")
        self.record_job("feature/x")
        code, out, err = self.fetch()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.git(self.host, "rev-parse", "review/p-feature-x"), tip)
        self.assertIn(f"fetching feature/x (the branch job {JOB} ended on) into review/p-feature-x", err)
        self.assertIn("git diff HEAD...review/p-feature-x", out)

    def test_an_old_review_branch_does_not_block_the_jobs_branch(self):
        self.git(self.host, "branch", "review/p")                 # an old, unrelated review/p
        self.git(self.box, "checkout", "-q", "-b", "feature/x")
        self.commit(self.box, "on the branch")
        self.record_job("feature/x")
        self.assertEqual(self.fetch()[0], 0)

    def test_no_branch_recorded_fetches_main(self):
        self.record_job(None)
        code, out, err = self.fetch("--json")
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual((d["ref"], d["branch"], d["ref_source"]), ("main", "review/p", "default"))
        self.assertIn("no branch recorded", err)

    def test_explicit_ref_and_branch_win(self):
        self.git(self.box, "checkout", "-q", "-b", "feature/x")
        self.commit(self.box, "on the branch")
        self.record_job("feature/x")
        code, out, err = self.fetch("--ref", "main", "--branch", "mine", "--json")
        self.assertEqual(code, 0, err)
        d = json.loads(out)
        self.assertEqual((d["ref"], d["branch"], d["ref_source"]), ("main", "mine", "flag"))

    def test_resolved_branch_checked_out_on_the_host_refuses(self):
        self.git(self.host, "checkout", "-q", "-b", "review/p-feature-x")
        self.record_job("feature/x")
        code, _, err = self.fetch()
        self.assertEqual(code, 2)
        self.assertIn("checked out", err)


class DashboardFetchTests(unittest.TestCase):
    def render(self, d, target, mode="confirm"):
        rows = ab.dash_sort(d["profiles"])
        ui = ab.dash_set_rows(ab.dash_new_ui((120, 30)), [r["name"] for r in rows], {r["name"]: r["vm"] for r in rows})
        return text_of(ab.dash_render(d, ui | {"mode": mode, "target": target}, ab.DashHistory(), (120, 30), now=NOW))

    def test_confirm_shows_ref_and_review_branch(self):
        d = data(row("a", j=job("finished", branch="feature/x")))
        text = self.render(d, "a")
        self.assertIn("agentbox fetch a", text)
        self.assertIn("fetches  feature/x", text)
        self.assertIn("into     review/a-feature-x", text)
        self.assertIn(f"the branch job {job()['id']} ended on", text)

    def test_confirm_without_a_branch_says_main(self):
        text = self.render(data(row("a", j=job("finished"))), "a")
        self.assertIn("fetches  main", text)
        self.assertIn("into     review/a ", text)

    def test_confirm_text_is_fetch_defaults(self):
        for j in (job("finished", branch="x/y"), job(), dict(ab.NO_JOB), None):
            t = ab.fetch_defaults("a", j)
            body = text_of([[seg for seg in ln] for ln in ab.dash_fetch_text(row("a", j=j), "a", True)])
            self.assertIn(t["ref"], body)
            self.assertIn(t["branch"], body)
            self.assertIn(t["why"], body)

    def test_stopped_sandbox_is_explained_not_offered(self):
        d = data(row("a", vm="stopped", j=job("finished", branch="feature/x")))
        rows = ab.dash_sort(d["profiles"])
        ui = ab.dash_set_rows(ab.dash_new_ui((120, 30)), ["a"], {"a": "stopped"})
        ui, act = ab.dash_key(ui, "f")
        self.assertEqual((ui["mode"], act), ("nofetch", None))
        text = text_of(ab.dash_render(d, ui, ab.DashHistory(), (120, 30), now=NOW))
        self.assertIn("is stopped. Fetch needs it running", text)
        self.assertNotIn("y to run it", text)
        for k in ("y", "Y", "enter", "f"):
            ui2, act = ab.dash_key(ui, k)
            self.assertEqual((ui2["mode"], act), ("list", None), k)
        self.assertEqual(ab.dash_key(ui, "q")[1], ("quit",))
        self.assertEqual(rows[0]["name"], "a")

    def test_missing_sandbox(self):
        ui = ab.dash_set_rows(ab.dash_new_ui((120, 30)), ["a"], {"a": "missing"})
        ui, act = ab.dash_key(ui, "f")
        self.assertEqual((ui["mode"], act), ("nofetch", None))
        self.assertIn("does not exist", self.render(data(row("a", vm="missing")), "a", mode="nofetch"))

    def test_running_sandbox_still_asks_and_runs_plain_fetch(self):
        ui = ab.dash_set_rows(ab.dash_new_ui((120, 30)), ["a"], {"a": "running"})
        ui, act = ab.dash_key(ui, "f")
        self.assertEqual(act, ("confirm_fetch", "a"))
        ui, act = ab.dash_key(ui, "y")
        self.assertEqual(ab.dash_argv(act), ["fetch", "a"])

    def test_detail_panel_shows_the_branch_sanitised(self):
        d = data(row("a", j=job("finished", branch="feature/x", end_head=HEAD)))
        rows = ab.dash_sort(d["profiles"])
        text = text_of(ab.dash_detail(rows[0], ab.DashHistory(), 80, NOW, False))
        self.assertIn("branch  feature/x  at 0123456789ab", text)
        evil = data(row("a", j=job("finished", branch="x\x1b]0;pwn\x07", end_head="nothex")))
        text = text_of(ab.dash_detail(ab.dash_sort(evil["profiles"])[0], ab.DashHistory(), 80, NOW, False))
        self.assertNotIn("branch", text)


class ProgressBranchTests(unittest.TestCase):
    def test_progress_line_and_json_carry_the_branch(self):
        acc = ab.new_acc(JOB)
        prof = mock.Mock(d={"stall_after": 300})
        j = {"id": JOB, "state": "finished", "started_at": None, "finished_at": None, "branch": "feature/x",
             "end_head": HEAD}
        view = ab.progress_view(prof, acc, j, {}, NOW)
        self.assertEqual((view["branch"], view["end_head"]), ("feature/x", HEAD))
        self.assertIn("branch feature/x at 0123456789ab", ab.progress_lines(JOB, "finished", view))
        view = ab.progress_view(prof, acc, j | {"branch": "-evil", "end_head": "x"}, {}, NOW)
        self.assertEqual((view["branch"], view["end_head"]), (None, None))
        self.assertFalse([ln for ln in ab.progress_lines(JOB, "finished", view) if ln.startswith("branch")])


@needs_linux_vm
@unittest.skipUnless(shutil.which("git"), "needs git")
class WrapperRecordsTheBranchTests(JobCase):
    def git(self, *args):
        with real_sh():
            r = REAL_RUN(["git", "-C", str(self.ws), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                         capture_output=True, text=True, env=self.vm.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_branch_and_head_end_up_in_the_job_record(self):
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        self.git("checkout", "-q", "-b", "feature/x")
        head = self.git("rev-parse", "HEAD")
        job_id = self.start()
        self.finish(job_id)
        d = self.vm.jobdir(job_id)
        self.assertEqual(((d / "branch").read_text().strip(), (d / "end_head").read_text().strip()), ("feature/x", head))
        info = ab.job_info(self.prof)
        self.assertEqual((info["branch"], info["end_head"]), ("feature/x", head))
        rec = ab.job_record(self.prof)
        self.assertEqual((rec["branch"], rec["end_head"]), ("feature/x", head))
        self.assertEqual(ab.public_job(ab.job_info(self.prof))["branch"], "feature/x")      # from the cache now

    def test_detached_head_records_no_branch(self):
        self.git("init", "-q", "-b", "main")
        self.git("commit", "-q", "--allow-empty", "-m", "base")
        self.git("checkout", "-q", "--detach")
        job_id = self.start()
        self.finish(job_id)
        info = ab.job_info(self.prof)
        self.assertIsNone(info["branch"])
        self.assertIsNotNone(info["end_head"])

    def test_a_hostile_branch_file_is_ignored(self):
        job_id = self.start()                                     # ws is not a git repo: empty files
        self.finish(job_id)
        (self.vm.jobdir(job_id) / "branch").write_text("--upload-pack=evil\n")
        ab.save_job_record(self.prof, {"id": job_id, "agent": "claude"})       # forget the cached answer
        self.assertIsNone(ab.job_info(self.prof)["branch"])


if __name__ == "__main__":
    unittest.main()
