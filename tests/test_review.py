"""`agentbox review`: items and statuses on real temporary repos, the git parsers, the renderer, the key handler,
the merge / delete actions and their refusals, and a pty end-to-end run."""
import json
import os
import random
import select
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

if sys.platform != "win32":
    import termios                # only the pty tests use it; they skip on Windows

from helpers import REAL_POPEN, REAL_RUN, AgentboxTestCase, ab, real_sh

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
T0 = 1767225600          # 2026-01-01T00:00:00Z


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


class GitCase(AgentboxTestCase):
    """A real host repo in self.ws (branch main), git allowed; every git argv review runs is recorded."""

    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            self.skipTest("needs git")
        env = mock.patch.dict(os.environ, GIT_ENV)
        env.start()
        self.addCleanup(env.stop)
        self.git_calls = []

        def run(cmd, *a, **kw):
            return REAL_RUN(cmd, *a, **kw)          # recorded by popen below: run starts a Popen

        def popen(cmd, *a, **kw):
            self.git_calls.append(list(cmd))
            return REAL_POPEN(cmd, *a, **kw)
        for name, fn in (("run", run), ("Popen", popen)):
            p = mock.patch.object(subprocess, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ab, "sandbox_state", lambda prof, states=None: "running")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(ab, "sandbox_states", lambda cfg: {})
        p.start()
        self.addCleanup(p.stop)
        self.clock = T0
        self.git(self.ws, "init", "-q", "-b", "main")
        self.write("a.txt", "one\ntwo\nthree\n")
        self.base = self.commit("base")

    def git(self, repo, *args, check=True):
        r = REAL_RUN(["git", "-C", str(repo), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
        if check:
            assert r.returncode == 0, (args, r.stderr)
        return r.stdout.strip()

    def write(self, name, text, repo=None):
        p = Path(repo or self.ws) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            p.write_bytes(text)
        else:
            p.write_text(text, newline="\n")      # no \r\n on Windows: the diffs are compared line by line

    def commit(self, subject, repo=None):
        self.clock += 60
        env = {"GIT_AUTHOR_DATE": f"@{self.clock} +0000", "GIT_COMMITTER_DATE": f"@{self.clock} +0000"}
        with mock.patch.dict(os.environ, env):
            self.git(repo or self.ws, "add", "-A")
            self.git(repo or self.ws, "commit", "-q", "--allow-empty", "-m", subject)
        return self.git(repo or self.ws, "rev-parse", "HEAD")

    def branch_with(self, name, start, *changes):
        """Branch `name` from `start` with one commit per (file, text) change; back on main after."""
        self.git(self.ws, "checkout", "-q", "-b", name, start)
        for i, (f, text) in enumerate(changes):
            self.write(f, text)
            self.commit(f"{name} change {i + 1}")
        tip = self.git(self.ws, "rev-parse", "HEAD")
        self.git(self.ws, "checkout", "-q", "main")
        return tip

    def merges(self):
        """git argv that change something: merge, branch, reset, rebase, push, checkout, cherry-pick..."""
        risky = {"merge", "branch", "reset", "rebase", "push", "checkout", "switch", "cherry-pick", "revert",
                 "update-ref", "tag", "stash", "commit", "am", "apply", "clean", "restore"}
        out = []
        for c in self.git_calls:
            if c and c[0] == "git" and "--no-pager" in c:          # review's own git, not the test's set-up
                sub = next((x for x in c[1:] if not x.startswith("-") and x not in (
                    "core.quotePath=true", "diff.noprefix=false", str(self.ws))), None)
                if sub in risky:
                    out.append(c[c.index(sub):])
        return out


class ItemTests(GitCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\nmode = "clone"\n'
                          f'[profiles.p-x]\nworkspace = "{self.ws}"\nmode = "clone"\n'
                          f'[profiles.d]\nworkspace = "{self.ws}"\n')
        self.prof = ab.Config().profile("p")
        self.merged_tip = self.branch_with("review/p-old", "main", ("b.txt", "b\n"))
        self.git(self.ws, "merge", "-q", "--ff-only", "review/p-old")
        self.div_tip = self.branch_with("review/p-div", "main", ("a.txt", "one\nTWO\nthree\n"))
        self.write("c.txt", "main moved\n")
        self.main_moved = self.commit("main moves")
        self.div2 = self.branch_with("tmp-div", self.base, ("d.txt", "x\n"))
        self.ready_tip = self.branch_with("review/p-feat", "main", ("e.txt", "e1\n"), ("e.txt", "e2\n"))
        self.other = self.branch_with("review/p-x-thing", "main", ("f.txt", "f\n"))
        self.git(self.ws, "branch", "-q", "-D", "tmp-div")
        jobs = [("20260101T010000Z-000001", "feat", self.ready_tip, "Add e", 2),
                ("20260101T020000Z-000002", "newwork", "b" * 40, "New work", 4),
                ("20260101T030000Z-000003", "nocommits", "c" * 40, "Nothing", 0),
                ("20260101T040000Z-000004", "old", self.merged_tip, "Old", 1)]
        for i, (jid, br, end, title, n) in enumerate(jobs):
            rec = {"id": jid, "state": "finished", "exit_code": 0, "started_at": iso(T0 + 3600 * (i + 1)),
                   "finished_at": iso(T0 + 3600 * (i + 1) + 600), "branch": br, "end_head": end, "title": title,
                   "progress": ab.new_acc(jid) | {"commits": n}}
            ab.append_history(self.prof, ab.history_record(rec))
        ab.save_job_record(self.prof, {"id": "20260101T020000Z-000002", "state": "finished"})

    def items(self):
        return {i["branch"]: i for i in ab.review_profile(self.prof, ab.Config().profile_names(), {})["items"]}

    def test_every_status(self):
        items = self.items()
        self.assertEqual({b: i["status"] for b, i in items.items()},
                         {"review/p-old": "merged", "review/p-div": "diverged", "review/p-feat": "ready",
                          "review/p-newwork": "not_fetched"})
        self.assertEqual(items["review/p-feat"]["ahead"], 2)
        self.assertEqual(items["review/p-feat"]["title"], "Add e")
        self.assertEqual(items["review/p-feat"]["target"], "main")
        self.assertEqual(items["review/p-newwork"]["kind"], "job")
        self.assertEqual(items["review/p-newwork"]["ref"], "newwork")
        self.assertTrue(items["review/p-newwork"]["latest"])
        self.assertEqual(ab.review_fetch_argv(items["review/p-newwork"]), ["fetch", "p"])
        self.assertEqual(ab.review_fetch_argv(items["review/p-newwork"] | {"latest": False}),
                         ["fetch", "p", "--ref", "newwork", "--branch", "review/p-newwork"])
        self.assertIsNone(ab.review_fetch_argv(items["review/p-feat"]))

    def test_branches_go_to_the_longest_profile_name(self):
        self.assertNotIn("review/p-x-thing", self.items())
        other = ab.review_profile(ab.Config().profile("p-x"), ab.Config().profile_names(), {})
        self.assertEqual([i["branch"] for i in other["items"]], ["review/p-x-thing"])
        self.assertEqual(ab.review_owner("review/p", ["p", "p-x"]), "p")
        self.assertEqual(ab.review_owner("review/p-x", ["p", "p-x"]), "p-x")
        self.assertIsNone(ab.review_owner("review/q", ["p"]))

    def test_actionable_first_newest_first(self):
        items = ab.review_profile(self.prof, ["p", "p-x"], {})["items"]
        self.assertEqual(items[-1]["status"], "merged")
        whens = [i["when"] for i in items[:-1]]
        self.assertEqual(whens, sorted(whens, reverse=True))

    def test_target_is_the_checked_out_branch(self):
        self.git(self.ws, "checkout", "-q", "-b", "dev", self.base)
        items = self.items()
        self.assertEqual(items["review/p-feat"]["target"], "dev")
        self.assertEqual(items["review/p-old"]["status"], "ready")         # old isn't in dev
        self.git(self.ws, "checkout", "-q", "--detach", "main")
        self.assertIsNone(self.items()["review/p-feat"]["target"])

    def test_json(self):
        code, out, err = self.run_main("review", "--json")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual([p["name"] for p in data["profiles"]], ["p", "p-x"])     # clone mode only
        p = data["profiles"][0]
        self.assertEqual(p["target"], "main")
        self.assertEqual(p["counts"], {"ready": 1, "not_fetched": 1, "diverged": 1, "merged": 1})
        self.assertNotIn("summary", json.dumps(p))
        self.assertEqual({i["status"] for i in p["items"]}, {"ready", "not_fetched", "diverged", "merged"})
        code, out, err = self.run_main("review", "p-x", "--json")
        self.assertEqual([p["name"] for p in json.loads(out)["profiles"]], ["p-x"])
        self.assertEqual(self.run_main("review", "d", "--json")[0], 2)       # direct mode
        self.assertEqual(self.merges(), [])

    def test_needs_a_terminal(self):
        code, out, err = self.run_main("review", "p")
        self.assertEqual(code, 2)
        self.assertIn("needs a terminal", err)

    def test_not_a_repo(self):
        # git makes objects read-only, which Windows refuses to delete: clear the flag and retry
        shutil.rmtree(self.ws / ".git", onerror=lambda fn, path, _: (os.chmod(path, 0o700), fn(path)))
        p = ab.review_profile(self.prof, ["p"], {})
        self.assertIn("not a git repository", p["error"])
        self.assertEqual(p["items"], [])

    def test_detail_is_the_merge_base_diff(self):
        d = ab.review_detail(self.items()["review/p-div"])
        self.assertIsNone(d["error"])
        self.assertEqual([f["path"] for f in d["view"]["files"]], ["a.txt"])     # not main's c.txt
        self.assertEqual(len(d["commits"]), 1)
        self.assertIn(("add", "+TWO"), d["view"]["lines"])
        r = ab.review_detail(self.items()["review/p-feat"])
        self.assertEqual([c["subject"] for c in r["commits"]], ["review/p-feat change 2", "review/p-feat change 1"])
        v = ab.review_commit_view(self.items()["review/p-feat"], r["commits"][0]["hash"])
        self.assertIn(("del", "-e1"), v["lines"])
        self.assertEqual(ab.review_detail(self.items()["review/p-newwork"])["commits"], [])

    def test_dashboard_summary(self):
        s = ab.review_summary(ab.Config(), self.prof, {})
        self.assertEqual(s["counts"]["ready"], 1)
        self.assertEqual(s["commits"], 2 + 1 + 4)          # ready + diverged + the not-fetched job's count
        self.assertIsNone(ab.review_summary(ab.Config(), ab.Config().profile("d"), {}))
        note = ab.dash_review_note(s["counts"], s["commits"])
        self.assertIn("7 commits ready to review", note)
        self.assertIn("1 not fetched", note)
        self.assertEqual(ab.dash_review_note({"merged": 2}, 0), "")


class MergeTests(GitCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\nmode = "clone"\n')
        self.prof = ab.Config().profile("p")

    def item(self, branch):
        return next(i for i in ab.review_profile(self.prof, ["p"], {})["items"] if i["branch"] == branch)

    def test_fast_forward(self):
        tip = self.branch_with("review/p-ff", "main", ("n.txt", "n\n"))
        it = self.item("review/p-ff")
        chk = ab.review_merge_check(it)
        self.assertEqual((chk["ok"], chk["ff"], chk["count"]), (True, True, 1))
        self.assertEqual(chk["argv"], ["merge", "--ff-only", "--", "review/p-ff"])
        self.write("untracked.txt", "fine\n")                                   # untracked files are fine
        res = ab.review_merge(it, True)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["msg"], "merged; t opens a shell to run install/tests")
        self.assertIn(tip[:12], res["lines"][0])
        self.assertEqual(self.git(self.ws, "rev-parse", "main"), tip)
        self.assertEqual(self.merges(), [["merge", "--ff-only", "--", "review/p-ff"]])
        self.assertEqual(self.item("review/p-ff")["status"], "merged")

    def test_refuses_a_dirty_tree(self):
        self.branch_with("review/p-ff", "main", ("n.txt", "n\n"))
        it = self.item("review/p-ff")
        self.write("a.txt", "changed\n")
        chk = ab.review_merge_check(it)
        self.assertFalse(chk["ok"])
        self.assertIn("uncommitted changes", chk["reason"])
        self.git(self.ws, "add", "a.txt")                                        # staged counts too
        self.assertIn("uncommitted changes", ab.review_merge(it, True)["msg"])
        self.assertEqual(self.merges(), [])

    def test_refuses_a_merge_in_progress(self):
        self.branch_with("review/p-ff", "main", ("n.txt", "n\n"))
        it = self.item("review/p-ff")
        for name, what in (("MERGE_HEAD", "a merge"), ("CHERRY_PICK_HEAD", "a cherry-pick"), ("rebase-merge", "a rebase")):
            p = self.ws / ".git" / name
            p.mkdir() if name == "rebase-merge" else p.write_text(self.base + "\n")
            chk = ab.review_merge_check(it)
            self.assertFalse(chk["ok"])
            self.assertIn(f"{what} is in progress", chk["reason"])
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        self.assertTrue(ab.review_merge_check(it)["ok"])
        self.assertEqual(self.merges(), [])

    def test_refuses_when_things_moved(self):
        self.branch_with("review/p-ff", "main", ("n.txt", "n\n"))
        it = self.item("review/p-ff")
        self.branch_with("review/p-ff2", "review/p-ff", ("m.txt", "m\n"))
        self.git(self.ws, "branch", "-q", "-f", "review/p-ff", "review/p-ff2")
        self.assertIn("has moved", ab.review_merge_check(it)["reason"])
        self.git(self.ws, "checkout", "-q", "--detach", "main")
        self.assertIn("detached", ab.review_merge_check(it)["reason"])
        self.assertFalse(ab.review_merge_check(it | {"status": "merged"})["ok"])
        self.assertFalse(ab.review_merge_check(it | {"branch": "--force"})["ok"])
        self.assertEqual(self.merges(), [])

    def test_diverged_makes_a_merge_commit(self):
        tip = self.branch_with("review/p-div", "main", ("n.txt", "n\n"))
        self.write("o.txt", "o\n")
        self.commit("main moves")
        it = self.item("review/p-div")
        self.assertEqual(it["status"], "diverged")
        chk = ab.review_merge_check(it)
        self.assertEqual((chk["ok"], chk["ff"]), (True, False))
        self.assertFalse(ab.review_merge(it, True)["ok"])                       # an ff that isn't one: refused
        res = ab.review_merge(it, False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.git(self.ws, "rev-parse", "HEAD^2"), tip)
        self.assertEqual(self.merges(), [["merge", "--no-ff", "--no-edit", "--", "review/p-div"]])

    @unittest.skipIf(sys.platform == "win32", "Windows can't create a file name with a newline or escape")
    def test_conflict_is_aborted_at_once(self):
        self.branch_with("review/p-div", "main", ("a.txt", "agent\n"), ("we\nird\x1b[2J.txt", "x\n"))
        self.write("a.txt", "owner\n")
        self.write("we\nird\x1b[2J.txt", "y\n")
        head = self.commit("owner edits")
        it = self.item("review/p-div")
        res = ab.review_merge(it, False)
        self.assertFalse(res["ok"])
        self.assertIn("merge conflict in 2 file(s): aborted", res["msg"])
        self.assertIn("  a.txt", res["lines"])
        self.assertTrue(any("ird" in ln for ln in res["lines"]))
        self.assertEqual(self.merges(), [["merge", "--no-ff", "--no-edit", "--", "review/p-div"], ["merge", "--abort"]])
        self.assertEqual(self.git(self.ws, "rev-parse", "HEAD"), head)
        self.assertFalse((self.ws / ".git" / "MERGE_HEAD").exists())
        self.assertEqual(self.git(self.ws, "status", "--porcelain", "--untracked-files=no"), "")
        # what the UI draws of it is clean
        rv = ab.review_merge_prompt(ab.review_new_ui((100, 30)), it, {"ok": False, "reason": res["msg"]})
        rv = {**rv, "box": ("merge", res["lines"]), "items": [it]}
        text = "".join(t for ln in ab.review_render(rv, {}, (100, 30)) for t, _ in ln)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\n", text)

    def test_delete_only_merged(self):
        self.branch_with("review/p-ready", "main", ("n.txt", "n\n"))
        self.branch_with("review/p-done", "main")
        ready, done = self.item("review/p-ready"), self.item("review/p-done")
        self.assertEqual((ready["status"], done["status"]), ("ready", "merged"))
        self.assertFalse(ab.review_delete(ready)["ok"])
        self.assertFalse(ab.review_delete(ready | {"status": "merged"})["ok"])   # checked again, not believed
        self.assertEqual(self.merges(), [])
        res = ab.review_delete(done)
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.merges(), [["branch", "-d", "--", "review/p-done"]])
        self.assertEqual(self.git(self.ws, "branch", "--list", "review/p-done"), "")
        self.assertEqual(ab.review_delete_argv("-D"), None)


class ParserTests(GitCase):
    @unittest.skipIf(sys.platform == "win32", "Windows can't create a file name with a newline or escape")
    def test_hostile_names_and_binary_files(self):
        names = ["plain.txt", "new\nline.txt", "esc\x1b]2;pwned\x07.txt", "tab\there.txt", "sp ace.txt"]
        start = self.git(self.ws, "rev-parse", "HEAD")
        for n in names:
            self.write(n, "hello\n")
        bad = os.fsencode(str(self.ws)) + b"/bad-\xff\xfe.txt"
        try:
            with open(bad, "wb") as f:
                f.write(b"x\n")
        except OSError as e:            # APFS (macOS) refuses names that are not valid UTF-8 (EILSEQ)
            self.skipTest(f"this file system refuses non-UTF-8 file names: {e.strerror}")
        self.write("blob.bin", bytes(range(256)) * 4)
        self.git(self.ws, "mv", "a.txt", "renamed.txt")
        self.commit("hostile")
        num = ab.rgit(self.ws, "diff", *ab.REVIEW_RENDER, "-M", "--numstat", "-z", f"{start}...HEAD", "--")
        files = ab.parse_numstat_z(num.stdout)
        paths = {f["path"] for f in files}
        for n in names:
            self.assertIn(n, paths)
        self.assertIn(os.fsdecode(b"bad-\xff\xfe.txt"), paths)
        blob = next(f for f in files if f["path"] == "blob.bin")
        self.assertEqual((blob["binary"], blob["add"], blob["del"]), (True, None, None))
        ren = next(f for f in files if f["path"] == "renamed.txt")
        self.assertEqual(ren["old"], "a.txt")
        pat = ab.rgit(self.ws, "diff", *ab.REVIEW_RENDER, "-M", f"{start}...HEAD", "--")
        parsed = ab.parse_patch(pat.stdout)
        self.assertEqual(len(parsed), len(files))
        self.assertTrue(any(f["binary"] for f in parsed))
        view = ab.review_view(files, parsed)
        self.assertEqual(len(view["starts"]), len(files))
        self.assertIn(("note", "binary"), view["lines"])
        for _, text in view["lines"]:
            self.assertNotIn("\x1b", text)          # patch headers are C-quoted by git
            self.assertNotIn("\n", text)
        # drawn: nothing raw reaches the screen
        item = {"profile": "p", "kind": "branch", "branch": "review/p", "status": "ready", "tip": "a" * 40,
                "head": "b" * 40, "target": "main", "workspace": str(self.ws), "job": None, "title": None, "when": T0}
        rv = {**ab.review_new_ui((130, 40)), "items": [item], "tab": 2}
        lines = ab.review_render(rv, {"view": view, "detail": {"commits": [], "view": view, "error": None}}, (130, 40))
        for ln in lines:
            for t, _ in ln:
                self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or 0xd800 <= ord(c) < 0xe000 for c in t), repr(t))

    def test_numstat_parser_edge_cases(self):
        data = b"1\t2\tplain\x003\t0\t\x00old name\x00new\nname\x00-\t-\tbin\x00garbage\x00\x00"
        got = ab.parse_numstat_z(data)
        self.assertEqual([(f["path"], f["old"], f["add"], f["del"], f["binary"]) for f in got],
                         [("plain", None, 1, 2, False), ("new\nname", "old name", 3, 0, False), ("bin", None, None, None, True)])
        self.assertEqual(ab.parse_numstat_z(b"1\t1\t\x00only-old"), [])
        self.assertEqual(ab.parse_numstat_z(b""), [])

    def test_patch_parser_kinds_and_cap(self):
        patch = (b"diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n ctx\n-old\n"
                 b"+new\n--- not a header\n+++ nor this\n\\ No newline at end of file\n"
                 b"diff --git a/y b/y\nBinary files a/y and b/y differ\n")
        x, y = ab.parse_patch(patch)
        self.assertEqual([k for k, _ in x["lines"]], ["file"] * 4 + ["hunk", "ctx", "del", "add", "del", "add", "note"])
        self.assertTrue(y["binary"])
        big = b"diff --git a/z b/z\n@@ -0,0 +1,30 @@\n" + b"".join(b"+l%d\n" % i for i in range(30))
        [z] = ab.parse_patch(big, max_lines=10)
        self.assertEqual((len(z["lines"]), z["more"]), (10, 22))
        self.assertIn(("note", "(22 more lines; open in VS Code)"), ab.review_view([], [z])["lines"])

    def test_big_file_is_capped(self):
        start = self.git(self.ws, "rev-parse", "HEAD")
        self.write("big.txt", "".join(f"line {i}\n" for i in range(ab.REVIEW_FILE_LINES + 500)))
        self.commit("big")
        pat = ab.rgit(self.ws, "diff", *ab.REVIEW_RENDER, f"{start}...HEAD", "--")
        [f] = ab.parse_patch(pat.stdout)
        self.assertEqual(len(f["lines"]), ab.REVIEW_FILE_LINES)
        self.assertGreater(f["more"], 500)

    def test_log_parser(self):
        data = b"%s\x1f1767225600\x1fsubject \x1f with sep\x00\n%s\x1f\x1f\x00junk\x00" % (b"a" * 40, b"b" * 40)
        got = ab.parse_log_z(data)
        self.assertEqual(got[0], {"hash": "a" * 40, "time": 1767225600, "subject": "subject \x1f with sep"})
        self.assertEqual(got[1]["time"], None)
        self.assertEqual(len(got), 2)

    def test_git_argv_pins_the_format(self):
        argv = ab.review_git_argv("/r", "diff")
        self.assertEqual(argv[:6], ["git", "--no-pager", "-c", "core.quotePath=true", "-c", "diff.noprefix=false"])
        self.assertEqual(ab.REVIEW_RENDER, ("--no-color", "--no-ext-diff", "--no-textconv"))


def item(status="ready", profile="p", branch=None, **kw):
    base = {"profile": profile, "kind": "job" if status == "not_fetched" else "branch",
            "branch": branch or f"review/{profile}-{status}", "ref": status.replace("_", "-"), "status": status,
            "tip": "a" * 40, "head": "b" * 40, "ahead": 2, "title": f"{status} work", "when": T0,
            "job": {"id": "20260101T000000Z-abcdef", "title": f"{status} work", "summary": "I did it.", "cost_usd": 0.5,
                    "turns": 3, "tool_calls": 9, "denials": 0, "exit_code": 0, "started_at": iso(T0),
                    "finished_at": iso(T0 + 300), "state": "finished"},
            "latest": True, "target": "main", "workspace": "/ws", "vm": "running"}
    return {**base, **kw}


def view_of(nfiles=3, per=40):
    files = [{"path": f"f{i}.py", "old": None, "add": per, "del": 1, "binary": False} for i in range(nfiles)]
    patch = [{"lines": [("file", f"diff --git a/f{i}.py b/f{i}.py"), ("hunk", "@@ -1 +1 @@")]
              + [("add", f"+line {j} of f{i}") for j in range(per)] + [("hunk", "@@ -9 +9 @@"), ("del", "-gone")],
              "binary": False, "more": 0} for i in range(nfiles)]
    return ab.review_view(files, patch)


COMMITS = [{"hash": c * 40, "time": T0, "subject": f"commit {c}"} for c in "abc"]


class KeyTests(unittest.TestCase):
    def ui(self, items=None, size=(120, 40)):
        items = items if items is not None else [item("ready"), item("not_fetched"), item("diverged"), item("merged")]
        return ab.review_set_items(ab.review_new_ui(size), items)

    def ctx(self):
        v = view_of()
        return {"view": v, "commits": COMMITS, "detail": {"commits": COMMITS, "view": v}}

    def keys(self, rv, *keys):
        actions = []
        for k in keys:
            rv, a = ab.review_key(rv, k, self.ctx())
            if a:
                actions.append(a)
        return rv, actions

    def test_select_and_tabs(self):
        rv, _ = self.keys(self.ui(), "j", "j")
        self.assertEqual(rv["sel"], 2)
        rv, _ = self.keys(rv, "k", "end", "home")
        self.assertEqual(rv["sel"], 0)
        rv, _ = self.keys(rv, "tab")
        self.assertEqual((rv["tab"], rv["focus"]), (1, "pane"))
        rv, _ = self.keys(rv, "j", "j", "j", "j")
        self.assertEqual((rv["sel"], rv["csel"]), (0, 2))                   # the pane has the keys now
        rv, _ = self.keys(rv, "3", "btab", "1", "left", "j")
        self.assertEqual((rv["tab"], rv["focus"], rv["sel"]), (0, "list", 1))

    def test_enter_on_a_commit_shows_its_diff(self):
        rv, _ = self.keys(self.ui(), "2", "j", "enter")
        self.assertEqual((rv["tab"], rv["source"]), (2, "b" * 40))
        rv, _ = self.keys(rv, "a")
        self.assertIsNone(rv["source"])
        rv, _ = self.keys(rv, "2", "left", "j")                             # a new item: the whole diff again
        self.assertEqual((rv["source"], rv["csel"]), (None, 0))

    def test_diff_scrolling_files_hunks_and_search(self):
        v = self.ctx()["view"]
        rv, _ = self.keys(self.ui(), "3", "down", "down")
        self.assertEqual(rv["dtop"], 2)
        rv, _ = self.keys(rv, "n")
        self.assertEqual(rv["dtop"], v["starts"][1])
        rv, _ = self.keys(rv, "n", "N")
        self.assertEqual(rv["dtop"], v["starts"][1])
        rv, _ = self.keys(rv, "]")
        self.assertEqual(rv["dtop"], v["hunks"][2])
        rv, _ = self.keys(rv, "]", "[")
        self.assertEqual(rv["dtop"], v["hunks"][2])
        rv, _ = self.keys(rv, "pgdn")
        self.assertGreater(rv["dtop"], v["hunks"][2])
        rv, _ = self.keys(rv, "home", "/", "o", "f", " ", "f", "2", "enter")
        self.assertEqual(v["lines"][rv["dtop"]][1], "+line 0 of f2")
        rv, _ = self.keys(rv, "/", "enter")                                 # repeat
        self.assertEqual(v["lines"][rv["dtop"]][1], "+line 1 of f2")
        rv, _ = self.keys(rv, "/", "z", "z", "z", "enter")
        self.assertIn("not found", rv["msg"])
        rv, _ = self.keys(rv, "/", "x", "esc")
        self.assertEqual(rv["mode"], "list")
        rv, _ = self.keys(rv, "w")
        self.assertTrue(rv["wrap"])
        rv, _ = self.keys(rv, "end")
        self.assertLessEqual(rv["dtop"], len(v["lines"]) - 1)

    def test_fetch_needs_y_and_a_running_sandbox(self):
        rv, acts = self.keys(self.ui(), "j", "f", "n")
        self.assertEqual((acts, rv["msg"]), ([], "fetch cancelled"))
        rv, acts = self.keys(rv, "f", "y")
        self.assertEqual(acts, [("fetch", item("not_fetched"))])
        stopped = self.ui([item("not_fetched", vm="stopped")])
        rv, acts = self.keys(stopped, "f")
        self.assertEqual((rv["mode"], acts), ("nofetch", []))
        rv, acts = self.keys(rv, "y")
        self.assertEqual((rv["mode"], acts), ("list", []))
        rv, acts = self.keys(self.ui(), "f")                               # ready: nothing to fetch
        self.assertEqual(acts, [])
        self.assertIn("not fetched", rv["msg"])

    def test_merge_asks_then_runs_only_after_y(self):
        rv, acts = self.keys(self.ui(), "m")
        self.assertEqual(acts, [("merge_check", item("ready"))])
        chk = {"ok": True, "ff": True, "count": 2, "argv": ["merge", "--ff-only", "--", "review/p-ready"]}
        rv = ab.review_merge_prompt(rv, item("ready"), chk)
        self.assertEqual(rv["mode"], "merge")
        text = "".join(t for ln in ab.review_render(rv, self.ctx(), (120, 40)) for t, _ in ln)
        self.assertIn("git merge --ff-only -- review/p-ready", text)
        self.assertIn("by 2 commits", text)
        r2, acts = self.keys(rv, "n")
        self.assertEqual((acts, r2["msg"]), ([], "merge cancelled"))
        r2, acts = self.keys(rv, "y")
        self.assertEqual(acts, [("merge", item("ready"), True)])

    def test_merge_commit_needs_merge_typed(self):
        chk = {"ok": True, "ff": False, "count": 3, "argv": ["merge", "--no-ff", "--no-edit", "--", "review/p-diverged"]}
        rv = ab.review_merge_prompt(self.ui(), item("diverged"), chk)
        self.assertEqual(rv["mode"], "mergetyped")
        text = "".join(t for ln in ab.review_render(rv, self.ctx(), (120, 40)) for t, _ in ln)
        self.assertIn("git merge --no-ff --no-edit -- review/p-diverged", text)
        self.assertIn("Type merge", text)
        for keys in (("y",), ("m", "e", "r", "g", "enter"), ("M", "E", "R", "G", "E", "enter"), ("merge!", "enter"),
                     ("esc",), ("ctrl-c",)):
            _, acts = self.keys(rv, *keys)
            self.assertEqual(acts, [], keys)
        r2, acts = self.keys(rv, "m", "e", "r", "g", "x", "backspace", "e", "enter")
        self.assertEqual(acts, [("merge", item("diverged"), False)])
        self.assertEqual(r2["mode"], "list")

    def test_refusal_shows_the_reason(self):
        rv = ab.review_merge_prompt(self.ui(), item("ready"), {"ok": False, "reason": "/ws has uncommitted changes"})
        self.assertEqual(rv["mode"], "box")
        text = "".join(t for ln in ab.review_render(rv, self.ctx(), (120, 40)) for t, _ in ln)
        self.assertIn("uncommitted changes", text)
        _, acts = self.keys(rv, "y")
        self.assertEqual(acts, [])

    def test_merge_and_delete_only_where_they_apply(self):
        rv, acts = self.keys(self.ui(), "end", "m")                          # merged item
        self.assertEqual(acts, [])
        rv, acts = self.keys(self.ui(), "j", "m")                            # not fetched
        self.assertEqual(acts, [])
        for i in range(3):                                                   # ready, not fetched, diverged
            rv, acts = self.keys(self.ui(), *(["j"] * i), "d", "y")
            self.assertEqual(acts, [], i)
        rv, acts = self.keys(self.ui(), "end", "d")
        self.assertEqual(rv["mode"], "delete")
        text = "".join(t for ln in ab.review_render(rv, self.ctx(), (120, 40)) for t, _ in ln)
        self.assertIn("git branch -d -- review/p-merged", text)
        _, acts = self.keys(rv, "n")
        self.assertEqual(acts, [])
        _, acts = self.keys(rv, "y")
        self.assertEqual(acts, [("delete", item("merged"))])

    def test_code_shell_refresh_help_quit(self):
        _, acts = self.keys(self.ui(), "o", "t", "r", "q")
        self.assertEqual([a[0] for a in acts], ["code", "shell", "refresh", "quit"])
        rv, acts = self.keys(self.ui(), "j", "o")
        self.assertEqual(acts, [])
        rv, acts = self.keys(self.ui(), "?")
        self.assertEqual(rv["mode"], "help")
        rv, acts = self.keys(rv, "x")
        self.assertEqual((rv["mode"], acts), ("list", []))
        _, acts = self.keys(self.ui([]), "m", "d", "f", "o", "t", "enter", "j")
        self.assertEqual(acts, [])

    def test_no_key_sequence_merges_or_deletes_without_the_confirm(self):
        rng = random.Random(7)
        keys = list("jkmdfyntoqrwaN[]/123?hl") + ["merge", "enter", "esc", "tab", "btab", "up", "down", "pgdn",
                                                     "backspace", ("click", 5, 3), ("wheel", 1, 60, 10), "resize"]
        for _ in range(300):
            rv, prev_modes = self.ui(), []
            for _ in range(40):
                k = rng.choice(keys)
                seq = list(k) if k == "merge" else [k]
                for kk in seq:
                    mode = rv["mode"]
                    rv, a = ab.review_key(rv, kk, self.ctx())
                    if a and a[0] == "merge_check":
                        rv = ab.review_merge_prompt(rv, a[1], {"ok": True, "ff": a[1]["status"] == "ready", "count": 1,
                                                                "argv": []})
                    elif a and a[0] == "merge":
                        self.assertEqual(a[1]["status"], "ready" if a[2] else "diverged")
                        self.assertEqual(mode, "merge" if a[2] else "mergetyped")
                        self.assertEqual(kk, "y" if a[2] else "enter")
                    elif a and a[0] == "delete":
                        self.assertEqual((a[1]["status"], mode, kk), ("merged", "delete", "y"))
                    elif a and a[0] == "fetch":
                        self.assertEqual((a[1]["status"], mode, kk), ("not_fetched", "fetch", "y"))
                    elif a:
                        self.assertIn(a[0], ("quit", "refresh", "redraw", "code", "shell"))

    def test_mouse(self):
        rv = self.ui()
        lay = ab.review_rects(rv, 3)
        lx, ly, lw, lh = lay["list"]
        rv, _ = ab.review_key(rv, ("click", 2, ly + 5), self.ctx())        # rows 4-5: the third item
        self.assertEqual(rv["sel"], 2)
        px = lay["tabs"][0]
        spans = ab.review_tab_spans(ab.review_tab_labels((3, 3)))
        rv, _ = ab.review_key(rv, ("click", px + spans[2][0] + 1, lay["tabs"][1]), self.ctx())
        self.assertEqual((rv["tab"], rv["focus"]), (2, "pane"))
        lay = ab.review_rects(rv, 3)
        fx, fy, fw, fh = lay["files"]
        rv, _ = ab.review_key(rv, ("click", fx + 2, fy + 1), self.ctx())
        self.assertEqual(rv["dtop"], self.ctx()["view"]["starts"][1])
        dx, dy, dw, dh = lay["diff"]
        rv2, _ = ab.review_key(rv, ("wheel", 1, dx + 2, dy + 2), self.ctx())
        self.assertEqual(rv2["dtop"], rv["dtop"] + 3)
        rv3, _ = ab.review_key(rv, ("wheel", 1, 2, ly + 1), self.ctx())     # the list scrolls, not the diff
        self.assertEqual(rv3["dtop"], rv["dtop"])

    def test_new_data_keeps_the_selection(self):
        rv, _ = self.keys(self.ui(), "j", "j")
        rv = ab.review_set_items(rv, [item("diverged"), item("ready")])
        self.assertEqual(rv["items"][rv["sel"]]["status"], "diverged")


class RenderTests(unittest.TestCase):
    def render(self, size, items=None, tab=0, ascii_=False, **kw):
        items = items if items is not None else [item("ready"), item("not_fetched"), item("diverged"), item("merged")]
        rv = {**ab.review_set_items(ab.review_new_ui(size), items), "tab": tab, **kw}
        v = view_of(4, 200)
        ctx = {"view": v, "commits": COMMITS, "detail": {"commits": COMMITS, "view": v, "error": None}, "data": {}}
        return ab.review_render(rv, ctx, size, now=T0 + 3600, ascii_=ascii_)

    def text(self, lines):
        return "\n".join("".join(t for t, _ in ln) for ln in lines)

    def test_every_line_is_exactly_the_width_at_every_size(self):
        for size in ((60, 15), (80, 24), (99, 30), (100, 30), (130, 40), (200, 60), (61, 50)):
            for tab in (0, 1, 2):
                for wrap in (False, True):
                    lines = self.render(size, tab=tab, wrap=wrap)
                    self.assertEqual(len(lines), size[1], (size, tab))
                    for ln in lines:
                        self.assertEqual(sum(ab.dash_cells(t) for t, _ in ln), size[0], (size, tab, ln))

    def test_too_small(self):
        self.assertIn("make the pane bigger", self.text(self.render((50, 10))))
        self.assertEqual(self.render((0, 0)), [])

    def test_overview(self):
        out = self.text(self.render((130, 40)))
        for want in ("ready work", "20260101T000000Z-abcdef", "$0.50", "3 commits, 4 files changed", "+800",
                     "the agent's own summary, not verified", "I did it.", "target main", "review/p-ready"):
            self.assertIn(want, out)

    def test_commits_and_diff_tabs(self):
        out = self.text(self.render((130, 40), tab=1))
        self.assertIn("aaaaaaaaa  commit a", out)
        out = self.text(self.render((130, 40), tab=2))
        self.assertIn("diff --git a/f0.py b/f0.py", out)
        self.assertIn("+200", out)
        wide = self.render((200, 40), tab=2)
        self.assertIn("│", self.text(wide).splitlines()[3])                  # the file list goes to the left

    def test_diff_styles(self):
        lines = self.render((130, 40), tab=2)
        styles = {t.strip(): st for ln in lines for t, st in ln if t.strip()}
        self.assertEqual(styles.get("+line 0 of f0"), "green")
        self.assertEqual(styles.get("@@ -1 +1 @@"), "cyan")
        self.assertEqual(styles.get("diff --git a/f0.py b/f0.py"), "bold")

    def test_long_lines_are_cut_with_a_marker_or_wrapped(self):
        long = {"profile": "p", "kind": "branch", "status": "ready"}
        v = ab.review_view([{"path": "x", "old": None, "add": 1, "del": 0, "binary": False}],
                           [{"lines": [("add", "+" + "x" * 500)], "binary": False, "more": 0}])
        rv = {**ab.review_set_items(ab.review_new_ui((100, 30)), [item("ready")]), "tab": 2}
        cut = self.text(ab.review_diff_lines(v, rv, 50, 5, False))
        self.assertIn("…", cut)
        self.assertEqual(len(cut.splitlines()), 1)
        wrapped = ab.review_diff_lines(v, {**rv, "wrap": True}, 50, 20, False)
        self.assertEqual(len(wrapped), 11)
        del long

    def test_hostile_text_is_sanitised(self):
        evil = item("ready", title="\x1b]52;c;eA==\x07title‮", branch="review/p-x",
                    job={**item()["job"], "summary": "\x1b[2Jwipe\r\nok\x9b"})
        for tab in (0, 1, 2):
            for ln in self.render((130, 40), items=[evil], tab=tab):
                for t, _ in ln:
                    self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or c == "‮" for c in t), repr(t))

    def test_ascii(self):
        for tab in (0, 1, 2):
            out = self.text(self.render((100, 30), tab=tab, ascii_=True))
            self.assertTrue(all(ord(c) < 128 for c in out), [c for c in out if ord(c) >= 128][:5])

    def test_footer_follows_the_item(self):
        self.assertIn("m merge", self.text(self.render((130, 40)))[-200:])
        out = self.text(self.render((130, 40), items=[item("merged")]))
        self.assertIn("d delete", out.splitlines()[-1])
        out = self.text(self.render((130, 40), items=[item("not_fetched")]))
        self.assertIn("f fetch", out.splitlines()[-1])
        self.assertIn("nothing to review", self.text(self.render((130, 40), items=[])))

    def test_help_lists_every_action(self):
        out = self.text(self.render((130, 40), mode="help"))
        for k in ("fetch", "merge", "delete", "VS Code", "shell", "search", "wrap", "hunk"):
            self.assertIn(k, out)


class TitleAndRexTests(unittest.TestCase):
    def test_title_text(self):
        self.assertEqual(ab.review_title(item("ready")), "review: p/review/p-ready")
        self.assertEqual(ab.review_title(None, "p"), "review: p")
        self.assertEqual(ab.review_title(None), "review")
        evil = ab.review_title(item(branch="review/p-\x1b]2;x\x07‮̀"))
        self.assertFalse(any(ord(c) < 32 or 0x7f <= ord(c) < 0xa0 or c in "‮̀" for c in evil), evil)
        osc = ab.osc_title("a\x1bb\x07c")
        self.assertEqual(osc, "\x1b]2;abc\x1b\\")
        self.assertEqual((ab.TITLE_PUSH, ab.TITLE_POP), ("\x1b[22;0t", "\x1b[23;0t"))

    def test_record_is_idle_with_a_count_or_cleared(self):
        self.assertEqual(ab.review_rex_fields(3), {"state": "idle", "msg": "3 to review"})
        self.assertIsNone(ab.review_rex_fields(0))
        import io
        buf = io.StringIO()
        rex = ab.RexStatus(True)
        with mock.patch.object(sys, "stdout", buf):
            ab.review_rex(rex, 2)
            ab.review_rex(rex, 2)                   # unchanged: not sent again
            ab.review_rex(rex, 1)
            ab.review_rex(rex, 0)
            ab.review_rex(rex, 0)
        out = buf.getvalue()
        import base64
        self.assertEqual(out.count("\x1b]7501;"), 3)
        self.assertIn("\x1b]7501;state=idle:app=agentbox:id=review:msg=" + base64.b64encode(b"2 to review").decode(), out)
        self.assertIn("\x1b]7501;state=clear:id=review\x1b\\", out)
        for bad in ("blocked", "done", "error", "working"):
            self.assertNotIn(f"state={bad}", out)
        with mock.patch.object(sys, "stdout", io.StringIO()) as quiet:
            ab.review_rex(ab.RexStatus(False), 4)
        self.assertEqual(quiet.getvalue(), "")


class ShellAndTabTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sh = os.path.join(self.tmp.name, "zsh")
        with open(self.sh, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.sh, 0o755)

    def test_rex_shell_argv(self):
        ws = self.tmp.name              # absolute everywhere: since Python 3.13 "/ws" is not absolute on Windows
        self.assertEqual(ab.rex_shell_argv("/r/rex", ws, "p", self.sh),
                         ["/r/rex", "run", "--shell=none", "--cwd", ws, "--label", "p shell", "--", self.sh, "-i"])
        for args in (("", ws, "p", self.sh), ("/r/rex", "ws", "p", self.sh), ("/r/rex", ws, "-p", self.sh),
                     ("/r/rex", ws, "p", "/nonexistent/sh"), ("/r/rex", ws, None, self.sh)):
            self.assertIsNone(ab.rex_shell_argv(*args), args)

    def screen(self, rexbin):
        from test_dashboard_interactive import FakeCurses
        s = ab.ReviewScreen(mock.Mock(), FakeCurses(), lambda: None, ab.RexStatus(False), False, "p")
        s.rexbin = rexbin
        return s

    def test_t_inside_rex_opens_a_tab_with_the_login_shell(self):
        s = self.screen("/r/rex")
        with mock.patch.object(ab, "login_shell", lambda: self.sh), mock.patch.object(ab.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            msg = s.open_shell(item(workspace=self.tmp.name))
        self.assertEqual(msg, "opened a shell in a new Rex tab")
        self.assertEqual(run.call_args.args[0], ["/r/rex", "run", "--shell=none", "--cwd", self.tmp.name, "--label",
                                                 "p shell", "--", self.sh, "-i"])

    def test_t_outside_rex_suspends_into_shell(self):
        s = self.screen(None)
        calls = []
        with mock.patch.dict(os.environ, {"SHELL": self.sh}), mock.patch.object(ab.subprocess, "run",
                                                                                 lambda *a, **k: calls.append((a, k))):
            with mock.patch.object(ab, "print", create=True):
                msg = s.open_shell(item(workspace=self.tmp.name))
        self.assertEqual(msg, "back from the shell")
        self.assertEqual(calls[0][0][0], [self.sh, "-i"])
        self.assertEqual(calls[0][1]["cwd"], self.tmp.name)

    def test_o_without_code_says_so(self):
        s = self.screen(None)
        with mock.patch.object(ab.shutil, "which", lambda c: None):
            self.assertIn("not on PATH", s.open_code(item()))
        argv = ab.review_code_argv("/bin/code", item())
        self.assertEqual(argv[argv.index("difftool"):], ["difftool", "--no-prompt", "--no-symlinks",
                                                         "--extcmd=/bin/code --wait --diff", "b" * 40 + "..." + "a" * 40, "--"])
        self.assertIsNone(ab.review_code_argv("/bin/code", item("not_fetched")))

    def test_dashboard_v(self):
        ui = ab.dash_set_rows(ab.dash_new_ui(tab=True), ["c", "d"], {"c": "running", "d": "running"}, {},
                              {"c": "clone", "d": "direct"})
        ui, a = ab.dash_key(ui, "v")
        self.assertEqual(a, ("review_tab", "c"))
        ui2 = ab.dash_set_rows(ab.dash_new_ui(tab=False), ["c"], {}, {}, {"c": "clone"})
        self.assertEqual(ab.dash_key(ui2, "v")[1], ("review", "c"))
        ui, a = ab.dash_key(ab.dash_key(ui, "j")[0], "v")
        self.assertIsNone(a)
        self.assertIn("not a clone-mode profile", ui["msg"])
        self.assertEqual(ab.dash_argv(("review", "c")), ["review", "c"])
        self.assertEqual(ab.dash_argv(("review_tab", "c")), ["review", "c"])
        self.assertIsNone(ab.dash_argv(("review", "--all")))

    def test_dashboard_detail_and_marker(self):
        row = {"name": "c", "enabled": True, "mode": "clone", "vm": "stopped", "agent": "claude", "job": None,
               "review": {"counts": {"ready": 1, "not_fetched": 1, "diverged": 0, "merged": 0}, "commits": 3}}
        text = "".join(t for ln in ab.dash_render({"profiles": [row]}, ab.dash_set_rows(ab.dash_new_ui((120, 30)), ["c"]),
                                                  ab.DashHistory(), (120, 30)) for t, _ in ln)
        self.assertIn("3 commits ready to review", text)
        self.assertIn("1 not fetched", text)
        self.assertIn("◆", text)
        row["review"] = {"counts": {"ready": 0, "not_fetched": 0, "diverged": 0, "merged": 2}, "commits": 0}
        text = "".join(t for ln in ab.dash_render({"profiles": [row]}, ab.dash_set_rows(ab.dash_new_ui((120, 30)), ["c"]),
                                                  ab.DashHistory(), (120, 30)) for t, _ in ln)
        self.assertNotIn("ready to review", text)
        self.assertNotIn("◆", text)


@unittest.skipUnless(hasattr(os, "fork") and sys.platform != "win32", "needs a pseudo-terminal")
class PtyTests(GitCase):
    """The real curses loop under a pseudo-terminal, on a real repo: open, switch tabs, scroll, merge, quit."""

    def read_until(self, fd, want: bytes, buf: bytearray, timeout=15.0, start=0) -> bool:
        end = time.monotonic() + timeout
        while want not in buf[start:] and time.monotonic() < end:
            r, _, _ = select.select([fd], [], [], 0.1)
            if r:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
        return want in buf[start:]

    def test_open_tabs_scroll_merge_quit(self):
        import fcntl
        import pty
        import struct
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\nmode = "clone"\n')
        tip = self.branch_with("review/p-feat", "main", *[(f"m{i}.txt", "".join(f"row {j}\n" for j in range(80)))
                                                          for i in range(3)])
        pid, fd = pty.fork()
        if pid == 0:
            code = 98
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
                os.environ.update(TERM="xterm-256color", REX_SESSION="test", **GIT_ENV)
                for k in ("AGENTBOX_NO_OSC", "REX_BLOCK", "NO_COLOR"):
                    os.environ.pop(k, None)
                with mock.patch.object(subprocess, "run", REAL_RUN), mock.patch.object(subprocess, "Popen", REAL_POPEN), \
                        mock.patch.object(sys, "argv", ["agentbox", "review", "p"]):
                    ab.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 97
            except BaseException:
                import traceback
                traceback.print_exc()
            finally:
                sys.stdout.flush()
                os._exit(code)
        self.addCleanup(os.close, fd)
        buf = bytearray()

        def press(key, want):
            start = len(buf)
            os.write(fd, key)
            if want:
                self.assertTrue(self.read_until(fd, want, buf, start=start), (key, bytes(buf[start:][-2000:])))
            else:
                time.sleep(0.3)
        self.assertTrue(self.read_until(fd, b"review/p-feat", buf), bytes(buf[-2000:]))
        press(b"2", b"review/p-feat change 3")
        press(b"3", b"diff --git")
        press(b"\x1b[B", None)                                   # down: scrolls the diff
        press(b"n", b"m1.txt")
        press(b"\x1b[6~", None)                                  # PgDn
        press(b"1", b"the agent's own summary")
        press(b"m", b"git merge --ff-only -- review/p-feat")
        press(b"y", b"t opens a shell")
        press(b"x", None)
        press(b"q", None)
        end, status = time.monotonic() + 15, None
        while time.monotonic() < end:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
            self.read_until(fd, b"\x00never", buf, timeout=0.1)
        else:
            self.allow_kill(pid)
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail("review did not exit after q")
        self.read_until(fd, b"\x00never", buf, timeout=0.3)
        out = bytes(buf)
        self.assertTrue(os.WIFEXITED(status), status)
        self.assertEqual(os.WEXITSTATUS(status), 0, out[-3000:])
        self.assertNotIn(b"Traceback", out)
        self.assertIn(b"\x1b[?1049h", out)
        self.assertIn(b"\x1b[?1049l", out[out.rindex(b"\x1b[?1049h"):])
        attrs = termios.tcgetattr(fd)
        self.assertTrue(attrs[3] & termios.ICANON and attrs[3] & termios.ECHO, "terminal left in raw mode")
        self.assertIn(b"\x1b[22;0t", out)                                     # title saved...
        self.assertIn(b"\x1b]2;review: p/review/p-feat\x1b\\", out)          # ...set...
        self.assertTrue(out.rstrip().endswith(b"\x1b[23;0t") or b"\x1b[23;0t" in out[out.rindex(b"\x1b[?1049l"):])
        self.assertIn(b"\x1b]7501;state=idle:app=agentbox:id=review", out)
        self.assertIn(b"\x1b]7501;state=clear:id=review\x1b\\", out)
        self.assertEqual(self.git(self.ws, "rev-parse", "main"), tip)       # the target moved


if __name__ == "__main__":
    unittest.main()
