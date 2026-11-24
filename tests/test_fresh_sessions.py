"""`fresh` keeps the agent's sessions; `down`/`fresh` refuse to drop clone-mode work the host lacks."""
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

from helpers import REAL_POPEN, REAL_RUN, AgentboxTestCase, LocalVM, ab, posix_only


def git(cwd, *args):
    r = REAL_RUN(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                 capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


class CpVM(LocalVM):
    """LocalVM plus `sbx cp` (either direction) and root `exec --user 0` (run locally, chown ignored)."""

    def __call__(self, cfg, *args, check=True, capture=False, stdin=None):
        done = lambda rc=0: subprocess.CompletedProcess(args, rc, "", "")
        if args[0] == "cp":
            self.calls.append(args)
            src, dst = args[1], args[2]
            if src.startswith("ab-p:"):
                s, d = Path(src[5:]), Path(dst)
            else:
                s, d = Path(src), Path(dst[5:])
            if not s.is_dir():
                return done(1)
            shutil.copytree(s, d / s.name, symlinks=True)
            return done()
        if args[:3] == ("exec", "--user", "0"):
            self.calls.append(args)
            if args[4] == "chown":
                return done()
            with mock.patch.object(subprocess, "Popen", REAL_POPEN):
                r = REAL_RUN(list(args[4:]), env=self.env, capture_output=True)
            return done(r.returncode)
        return super().__call__(cfg, *args, check=check, capture=capture, stdin=stdin)


@posix_only
class SessionsTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
            agent = "claude"
        ''')
        self.vm = CpVM(self)
        self.sessions = self.vm.home / ".claude" / "projects"
        p = mock.patch.dict(ab.SESSION_DIRS, {"claude": str(self.sessions)})
        p.start()
        self.addCleanup(p.stop)
        self.prof = ab.Config().profile("p")
        self.prof.state.mkdir(parents=True, exist_ok=True)

    def test_save_then_restore(self):
        (self.sessions / "-ws").mkdir(parents=True)
        (self.sessions / "-ws" / "s1.jsonl").write_text('{"x":1}\n')
        (self.sessions / "-ws" / "leak").symlink_to("/etc/passwd")      # agent-written: never kept
        self.assertIsNone(ab.save_sessions(self.prof))
        saved = self.prof.state / "sessions" / "projects"
        self.assertEqual((saved / "-ws" / "s1.jsonl").read_text(), '{"x":1}\n')
        self.assertFalse((saved / "-ws" / "leak").exists() or (saved / "-ws" / "leak").is_symlink())

        shutil.rmtree(self.sessions)                                     # the new VM has none
        (self.sessions / "stale").mkdir(parents=True)
        ab.restore_sessions(self.prof)
        self.assertEqual((self.sessions / "-ws" / "s1.jsonl").read_text(), '{"x":1}\n')
        self.assertFalse((self.sessions / "stale").exists())             # replaced, not merged
        self.assertFalse((self.prof.state / "sessions").exists())        # consumed once restored

    def test_nothing_to_save(self):
        self.assertIsNone(ab.save_sessions(self.prof))
        self.assertFalse((self.prof.state / "sessions").exists())
        ab.restore_sessions(self.prof)                                   # and nothing to restore
        self.assertFalse(any(c[0] == "cp" for c in self.vm.calls))

    def test_agents_without_a_session_dir_are_skipped(self):
        self.prof.d["agent"] = "shell"
        self.assertIsNone(ab.save_sessions(self.prof))
        self.assertEqual(self.vm.calls, [])

    def test_fresh_keeps_sessions_and_down_drops_them(self):
        (self.sessions / "-ws").mkdir(parents=True)
        (self.sessions / "-ws" / "s1.jsonl").write_text("{}\n")
        (self.prof.state / "token").write_text("t")
        args = mock.Mock(force=True, keep_state=False, keep_sessions=True)
        with mock.patch.object(ab, "job_info", lambda prof: {"state": "none"}), \
                mock.patch.object(ab, "remove_secrets", lambda prof: None):
            ab.cmd_down(self.prof, args)
            self.assertEqual(sorted(p.name for p in self.prof.state.iterdir()), ["sessions"])
            self.vm.exists = True
            args.keep_sessions = False
            ab.cmd_down(self.prof, args)
        self.assertFalse(self.prof.state.exists())

    def test_restore_hands_the_parent_to_the_agent(self):
        (self.sessions / "-ws").mkdir(parents=True)
        self.assertIsNone(ab.save_sessions(self.prof))
        ab.restore_sessions(self.prof)
        parent = str(self.sessions.parent)
        self.assertIn(("exec", "--user", "0", "ab-p", "chown", "1000:1000", parent), self.vm.calls)

    def test_force_removes_even_when_sessions_cannot_be_saved(self):
        args = mock.Mock(force=True, keep_state=False, keep_sessions=True)
        with mock.patch.object(ab, "job_info", lambda prof: {"state": "none"}), \
                mock.patch.object(ab, "remove_secrets", lambda prof: None), \
                mock.patch.object(ab, "save_sessions", lambda prof: "unreachable"):
            ab.cmd_down(self.prof, args)
            self.assertTrue(any(c[0] == "rm" for c in self.vm.calls))
            self.vm.exists = True
            args.force = False
            with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                    mock.patch.object(ab, "die", side_effect=SystemExit) as died:
                with self.assertRaises(SystemExit):
                    ab.cmd_down(self.prof, args)
            self.assertIn("could not save", died.call_args[0][0])


@posix_only
class CloneGuardTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        for name, real in (("run", REAL_RUN), ("Popen", REAL_POPEN)):     # host git
            p = mock.patch.object(subprocess, name, real)
            p.start()
            self.addCleanup(p.stop)
        git(self.ws, "init", "-q", "-b", "main")
        (self.ws / "a.txt").write_text("a\n")
        git(self.ws, "add", "a.txt")
        git(self.ws, "commit", "-q", "-m", "a")
        self.clone = self.tmp / "clone"
        REAL_RUN(["git", "clone", "-q", str(self.ws), str(self.clone)], check=True)
        self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
            mode = "clone"
        ''')
        self.vm = LocalVM(self)
        self.prof = ab.Config().profile("p")
        clone = str(self.clone)        # the VM sees the clone at vm_path(primary); here, a local dir
        p = mock.patch.object(ab, "vm_path", lambda path: clone)
        p.start()
        self.addCleanup(p.stop)

    def test_clean_clone_loses_nothing(self):
        self.assertEqual(ab.clone_unsaved_work(self.prof), [])

    def test_unfetched_commit_dirty_tree_and_stash(self):
        git(self.clone, "checkout", "-q", "-b", "feature")
        (self.clone / "b.txt").write_text("b\n")
        git(self.clone, "add", "b.txt")
        git(self.clone, "commit", "-q", "-m", "b")
        (self.clone / "a.txt").write_text("changed\n")
        lost = ab.clone_unsaved_work(self.prof)
        self.assertEqual(len(lost), 2, lost)
        self.assertIn("uncommitted", lost[0])
        self.assertIn("branch feature", lost[1])
        git(self.clone, "stash", "-q")
        self.assertTrue(any("stash" in x for x in ab.clone_unsaved_work(self.prof)))

    def test_a_fetched_branch_is_safe(self):
        git(self.clone, "commit", "-q", "--allow-empty", "-m", "c")
        self.assertTrue(ab.clone_unsaved_work(self.prof))
        git(self.ws, "fetch", "-q", str(self.clone), "main:from-vm")
        self.assertEqual(ab.clone_unsaved_work(self.prof), [])

    def test_missing_working_tree_is_not_reported_safe(self):
        shutil.rmtree(self.clone)
        lost = ab.clone_unsaved_work(self.prof)
        self.assertEqual(len(lost), 1, lost)
        self.assertIn("no git working tree", lost[0])

    def test_direct_mode_is_not_checked(self):
        self.prof.d["mode"] = "direct"
        self.assertEqual(ab.clone_unsaved_work(self.prof), [])
        self.assertEqual(self.vm.calls, [])

    def test_down_refuses_unless_forced(self):
        git(self.clone, "commit", "-q", "--allow-empty", "-m", "c")
        with mock.patch.object(sys.stdin, "isatty", return_value=True):
            code, _, err = self.run_main("down", "p")
        self.assertEqual(code, 1)
        self.assertIn("would lose", err)
        self.assertFalse(any(c[0] == "rm" for c in self.vm.calls))
        with mock.patch.object(ab, "job_info", lambda prof: {"state": "none"}), \
                mock.patch.object(ab, "remove_secrets", lambda prof: None):
            code, _, _ = self.run_main("down", "p", "--force")
        self.assertEqual(code, 0)
        self.assertTrue(any(c[0] == "rm" for c in self.vm.calls))
