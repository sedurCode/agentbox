"""Interactive agent sessions (`agentbox up`, Rex "Open Profile") on the dashboard, in Rex and in
`status --json`: the in-VM probe, its strict parser, the display states, rendering and Rex records."""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import REAL_POPEN, REAL_RUN, AgentboxTestCase, FakeSbx, ab, real_sh, needs_linux_vm
from test_dashboard import SAMPLE
from test_dashboard_interactive import NOW, data, job, row, text_of, ui_for
from test_rex import CommandCase, Tty, records

GOOD = """agent claude 812 10735 0
agent claude 3301 5769 0
agent opencode 4410 2243 0
agent claude 390 104 1
claude_activity 9 auto
opencode_activity 600
"""


def irow(name, age=9, sessions=1, mode="auto", j=None, vm="running", **kw):
    """A running VM with `sessions` interactive claude processes, the newest write `age` seconds ago."""
    return row(name, vm=vm, j=j if j is not None else dict(ab.NO_JOB),
               sessions=[{"agent": "claude", "age_s": 600 * (i + 1)} for i in range(sessions)],
               activity_age_s=age, permission_mode=mode, **kw)


def old_job(**kw):
    """A job that finished ten days before NOW."""
    return job("finished", finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 864000)), **kw)


class ParseTests(unittest.TestCase):
    def test_good_output(self):
        r = ab.parse_vm_sessions(GOOD)
        self.assertEqual(r, {"sessions": [{"agent": "claude", "age_s": 10735}, {"agent": "claude", "age_s": 5769},
                                          {"agent": "opencode", "age_s": 2243}],     # the job's claude is not one
                             "activity_age_s": 9, "permission_mode": "auto"})

    def test_stats_lines_around_it_change_nothing(self):
        self.assertEqual(ab.parse_vm_sessions(SAMPLE + GOOD), ab.parse_vm_sessions(GOOD))
        stats = ab.parse_vm_stats(SAMPLE + GOOD)
        self.assertEqual(stats, ab.parse_vm_stats(SAMPLE))
        self.assertEqual(ab.parse_vm_stats(SAMPLE + "\n\n   \n"), stats)      # blank lines don't break it

    def test_nothing(self):
        self.assertEqual(ab.parse_vm_sessions(""), ab.NO_SESSIONS)
        self.assertEqual(ab.parse_vm_sessions(SAMPLE), ab.NO_SESSIONS)

    def test_every_mode_and_unknown_ones(self):
        for m in ab.SESSION_MODES:
            self.assertEqual(ab.parse_vm_sessions(f"claude_activity 1 {m}\n")["permission_mode"], m)
        for m in ("-", "yolo", "AUTO", "auto\x1b[2J", "bypassPermissions;rm", ""):
            r = ab.parse_vm_sessions(f"claude_activity 7 {m}\n")
            self.assertEqual((r["permission_mode"], r["activity_age_s"]), (None, 7), m)

    def test_activity_is_the_newer_of_claude_and_opencode(self):
        self.assertEqual(ab.parse_vm_sessions("claude_activity 900 auto\nopencode_activity 30\n")["activity_age_s"], 30)
        self.assertEqual(ab.parse_vm_sessions("opencode_activity 30\n")["permission_mode"], None)

    def test_hostile_lines_are_ignored(self):
        bad = [
            "agent claude abc 10 0", "agent claude 12 ten 0", "agent claude -5 10 0", "agent claude 12 -10 0",
            "agent claude +12 10 0", "agent claude 1_2 10 0", "agent claude ١٢ 10 0", "agent claude 12 ١٠ 0",
            "agent claude 0 10 0", "agent claude 99999999999 10 0", "agent claude 4194305 10 0",
            "agent claude 12 99999999999 0", "agent claude 12 1000000001 0", "agent claude 12 10 2",
            "agent claude 12 10", "agent claude 12 10 0 extra", "agent bash 12 10 0", "agent Claude 12 10 0",
            "agent claude\x1b[2J 12 10 0", "agent claude 12 10 0\r", "agent  claude 12 10 0", "agent\tclaude 12 10 0",
            "agent claude 12 1e3 0", "agent claude 12 10.5 0", "agent claude 0x10 10 0",
            "claude_activity x auto", "claude_activity -1 auto", "claude_activity 99999999999 auto",
            "claude_activity 5", "claude_activity 5 auto extra", "opencode_activity", "opencode_activity 5 6",
            "opencode_activity 1e9", "\x1b]52;c;cHduZWQ=\x07", "agent", "", "x" * 100000,
        ]
        self.assertEqual(ab.parse_vm_sessions("\n".join(bad)), ab.NO_SESSIONS)

    def test_escape_sequences_never_reach_the_result(self):
        r = ab.parse_vm_sessions("agent claude 12 10 0\x1b[31m\nclaude_activity 3 auto\x1b]0;x\x07\n" + GOOD)
        self.assertNotIn("\x1b", json.dumps(r, ensure_ascii=False))
        self.assertEqual(len(r["sessions"]), 3)

    def test_truncated_output(self):
        for cut in range(len(GOOD)):
            r = ab.parse_vm_sessions(GOOD[:cut])
            self.assertLessEqual(len(r["sessions"]), 3)
            for s in r["sessions"]:
                self.assertIn(s["agent"], ab.SESSION_AGENTS)
                self.assertTrue(0 <= s["age_s"] <= ab.SESSION_SECS_MAX)
            self.assertIn(r["permission_mode"], (None, "auto"))
        self.assertEqual(ab.parse_vm_sessions("agent claude 812 107")["sessions"], [])      # cut before the job flag

    def test_huge_counts_are_capped(self):
        out = "".join(f"agent claude {p} 5 0\n" for p in range(2, 5000))
        self.assertEqual(len(ab.parse_vm_sessions(out)["sessions"]), ab.SESSIONS_MAX)
        self.assertEqual(len(ab.parse_vm_sessions("agent claude 7 5 0\n" * 100)["sessions"]), 1)   # same pid


class ScriptTests(AgentboxTestCase):
    def test_parses_as_posix_sh(self):
        if not shutil.which("sh"):
            self.skipTest("no sh")
        for script in (ab.VM_STATS_SCRIPT, ab.VM_SESSIONS_SCRIPT):
            with real_sh():
                r = REAL_RUN(["sh", "-n", "-c", script], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_job_id_only_for_a_job_that_may_be_running(self):
        prof = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n').profile("p")
        jid = "20261123T164235Z-dbb67c"
        self.assertTrue(ab.vm_script("X", prof).startswith("AB_JOB=\n"))            # no job
        for state, want in (("running", jid), ("finished", ""), ("lost", ""), (None, jid)):
            ab.save_job_record(prof, {"id": jid, "state": state})
            self.assertEqual(ab.vm_script("X", prof), f"AB_JOB={want}\nX", state)
        ab.save_job_record(prof, {"id": "x; rm -rf ~", "state": "running"})
        self.assertEqual(ab.vm_script("X", prof), "AB_JOB=\nX")

    @needs_linux_vm
    def test_the_real_script_finds_sessions_the_job_and_the_mode(self):
        """A fake HOME with a transcript and an opencode store, and two processes whose argv[0] is
        `claude`: one under a job's pid, one not."""
        home = self.tmp / "home"
        proj = home / ".claude" / "projects" / "-ws"
        proj.mkdir(parents=True)
        (proj / "old.jsonl").write_text('{"permissionMode":"plan"}\n')
        os.utime(proj / "old.jsonl", (time.time() - 5000, time.time() - 5000))
        secret = "TOP SECRET conversation text"
        line = lambda mode: json.dumps({"permissionMode": mode, "message": secret}, separators=(",", ":")) + "\n"
        (proj / "new.jsonl").write_text(line("default") + "x" * 70000 + "\n" + line("bypassPermissions"))
        oc = home / ".local" / "share" / "opencode"
        oc.mkdir(parents=True)
        (oc / "opencode.db").write_text("")
        os.utime(oc / "opencode.db", (time.time() - 300, time.time() - 300))
        nap = "import time; time.sleep(30)"           # python runs under any argv[0]: here "claude"
        quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "start_new_session": True}
        mine = REAL_POPEN(["/some/where/claude", "-c", nap], executable=sys.executable, **quiet)
        job_ = REAL_POPEN([sys.executable, "-c", "import subprocess, sys; subprocess.run(['claude', '-c', %r], "
                           "executable=sys.executable)" % nap], **quiet)
        for p in (mine, job_):                         # each in its own group: the job's child goes too
            self.allow_kill(p.pid)
            self.addCleanup(lambda p=p: (os.killpg(p.pid, signal.SIGKILL), p.wait()))
        jid = "20261123T164235Z-dbb67c"
        jdir = home / ".agentbox" / "jobs" / jid
        jdir.mkdir(parents=True)
        (jdir / "pid").write_text(f"{job_.pid}\n")
        time.sleep(0.3)
        with real_sh():
            r = REAL_RUN(["sh", "-c", f"AB_JOB={jid}\n" + ab.VM_SESSIONS_SCRIPT], capture_output=True, text=True,
                         env=dict(os.environ, HOME=str(home)))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(secret, r.stdout)
        lines = r.stdout.splitlines()
        self.assertTrue(any(re.fullmatch(rf"agent claude {mine.pid} [0-9] 0", ln) for ln in lines), r.stdout)
        job_lines = [ln for ln in lines if ln.startswith("agent claude ") and ln.endswith(" 1")]
        self.assertEqual(len(job_lines), 1, r.stdout)                # the job's child, found through its parent
        child = int(job_lines[0].split()[2])
        with open(f"/proc/{child}/stat") as f:
            self.assertEqual(int(f.read().rsplit(")", 1)[1].split()[1]), job_.pid)
        self.assertTrue(any(re.fullmatch(r"claude_activity [0-9] bypassPermissions", ln) for ln in lines), r.stdout)
        self.assertTrue(any(ln.startswith("opencode_activity 30") for ln in lines), r.stdout)
        for ln in lines:                                             # numbers and fixed words only
            self.assertRegex(ln, r"^(agent (claude|opencode) \d+ \d+ [01]|claude_activity \d+ [A-Za-z-]+"
                                 r"|opencode_activity \d+)$")
        parsed = ab.parse_vm_sessions(r.stdout)
        self.assertIn("claude", [s["agent"] for s in parsed["sessions"]])
        self.assertEqual(parsed["permission_mode"], "bypassPermissions")

    @needs_linux_vm
    def test_the_real_script_with_an_empty_home(self):
        home = self.tmp / "empty"
        home.mkdir()
        with real_sh():
            r = REAL_RUN(["sh", "-c", "AB_JOB=\n" + ab.VM_STATS_SCRIPT], capture_output=True, text=True,
                         env=dict(os.environ, HOME=str(home)))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIsNotNone(ab.parse_vm_stats(r.stdout))
        self.assertNotIn("_activity", r.stdout)


class StateTests(unittest.TestCase):
    def test_every_combination(self):
        cases = {
            "job running + interactive": (irow("a", j=job()), "working"),
            "job stalled + interactive": (irow("a", j=job(progress={"stalled": True})), "stalled"),
            "interactive working": (irow("a", age=90), "interactive_working"),
            "interactive idle": (irow("a", age=91), "interactive_idle"),
            "interactive, no activity known": (irow("a", age=None), "interactive_idle"),
            "old done job + interactive": (irow("a", age=5, j=old_job()), "interactive_working"),
            "old failed job + interactive": (irow("a", age=500, j=old_job(exit_code=2)), "interactive_idle"),
            "lost job + interactive": (irow("a", j=job("lost", finished_at=None)), "interactive_working"),
            "old done job, no sessions": (irow("a", sessions=0, j=old_job()), "done"),
            "no job, no sessions": (irow("a", sessions=0), "nojob"),
            "stopped VM with stale sessions": (irow("a", vm="stopped"), "stopped"),
            "none": (row("a", j=dict(ab.NO_JOB)), "nojob"),
        }
        for what, (r, want) in cases.items():
            self.assertEqual(ab.dash_state(r), want, what)

    def test_sort(self):
        d = data(irow("i-idle", age=900), irow("i-work"), row("w", j=job()), row("bad", j=job("finished", exit_code=1)),
                 row("ok", j=job("finished")), row("cold", vm="stopped", j=dict(ab.NO_JOB)),
                 irow("i-old-job", age=3, j=old_job()), irow("busy", j=job()))
        self.assertEqual([r["name"] for r in ab.dash_sort(d["profiles"])],
                         ["busy", "w", "i-old-job", "i-work", "bad", "i-idle", "ok", "cold"])

    def test_glyphs(self):
        work, idle = ab.DASH_STATES["interactive_working"], ab.DASH_STATES["interactive_idle"]
        self.assertNotEqual(work[2], ab.DASH_STATES["working"][2])
        self.assertEqual((work[2], idle[2]), ("blue", "blue"))
        glyphs = [g for g, *_ in ab.DASH_STATES.values()]
        ascii_ = [ga for _, ga, *_ in ab.DASH_STATES.values()]
        self.assertEqual((len(set(glyphs)), len(set(ascii_))), (len(glyphs), len(ascii_)))

    def test_time_column(self):
        self.assertEqual(ab.dash_when(irow("a", age=9, j=old_job()), NOW), "9s ago")
        self.assertEqual(ab.dash_when(irow("a", age=725, j=old_job()), NOW), "idle 12m")
        self.assertEqual(ab.dash_when(irow("a", age=None), NOW), "idle")
        self.assertEqual(ab.dash_when(irow("a", j=job()), NOW), "12m")       # the running job's elapsed time


class RenderTests(unittest.TestCase):
    def render(self, d, size=(120, 40), ascii_=False):
        return ab.dash_render(d, ui_for(d, size), ab.DashHistory(), size, now=NOW, ascii_=ascii_)

    def test_list_row(self):
        d = data(irow("spectrum", age=9, j=old_job()))
        ln = ab.dash_list(d["profiles"], ui_for(d) | {"sel": 5}, 60, 5, NOW, False)[0]
        self.assertIn(("◉", "blue"), ln)
        self.assertIn("9s ago", text_of([ln]))
        self.assertNotIn("ago", text_of([ab.dash_list(data(irow("s", age=999))["profiles"], ui_for(d), 60, 5, NOW,
                                                       False)[0]]).replace("idle 16m", ""))

    def test_detail_sessions_block_and_last_job(self):
        r = irow("spectrum", age=9, sessions=3, j=old_job())
        text = text_of(ab.dash_detail(r, ab.DashHistory(), 120, NOW, False))
        for want in ("spectrum  interactive", "sessions  3 interactive", "claude   running 10m",
                     "claude   running 30m", "last activity 9s ago", "mode auto", "last job: done 240h00m ago"):
            self.assertIn(want, text)
        self.assertNotIn("bypass", text)
        self.assertLess(text.index("sessions"), text.index("last job"))
        self.assertNotIn("tokens", text)                 # the old job is one line now
        lines = ab.dash_detail(r, ab.DashHistory(), 120, NOW, False)
        self.assertIn([("last job: done 240h00m ago", "dim"), ("   " + r["job"]["id"], "dim")], lines)

    def test_bypass_is_yellow(self):
        lines = ab.dash_detail(irow("a", mode="bypassPermissions"), ab.DashHistory(), 120, NOW, False)
        segs = [s for ln in lines for s in ln]
        self.assertIn(("bypassPermissions", "yellow"), segs)
        self.assertIn(("  (bypass: no permission checks)", "yellow"), segs)

    def test_unknown_mode_and_no_activity(self):
        text = text_of(ab.dash_detail(irow("a", age=None, mode="evil\x1b"), ab.DashHistory(), 120, NOW, False))
        self.assertIn("last activity -", text)
        self.assertNotIn("mode", text.split("last activity")[1].split("\n")[0])

    def test_running_job_keeps_its_full_block_under_the_sessions(self):
        text = text_of(ab.dash_detail(irow("a", j=job()), ab.DashHistory(), 120, NOW, False))
        self.assertIn("sessions  1 interactive", text)
        self.assertIn("job  20261123T164235Z-dbb67c  running", text)
        self.assertIn("tokens 4.1M", text)

    def test_whole_screen_every_size_and_ascii(self):
        d = data(irow("a", mode="bypassPermissions", j=old_job()), irow("b", age=4000), irow("c", j=job()))
        for size in ((120, 40), (70, 20), (60, 15)):
            for ascii_ in (False, True):
                lines = self.render(d, size, ascii_)
                self.assertEqual(len(lines), size[1])
                for ln in lines:
                    self.assertEqual(sum(ab.dash_cells(t) for t, _ in ln), size[0])
                if ascii_:
                    self.assertTrue(text_of(lines).isascii())

    def test_watch_card(self):
        d = {"generated_at": int(NOW), "sandboxes_known": True,
             "profiles": [irow("a", sessions=2, age=9, j=old_job()), irow("b", sessions=0)]}
        text = "\n".join(ab.dashboard_lines(d, ab.Style(False)))
        self.assertIn("interactive: 2 sessions, active 9s ago, mode auto", text)
        self.assertEqual(text.count("interactive:"), 1)
        coloured = "\n".join(ab.dashboard_lines(data(irow("a", mode="bypassPermissions")), ab.Style(True)))
        self.assertIn("\x1b[33mbypassPermissions\x1b[0m", coloured)
        self.assertIn("\x1b[34minteractive\x1b[0m: 1 session,", coloured)


class FakeCurses:
    error = Exception
    A_BOLD, A_DIM, A_REVERSE = 1, 2, 4
    COLOR_BLACK, COLOR_RED, COLOR_GREEN, COLOR_YELLOW, COLOR_BLUE, COLOR_CYAN = range(6)
    BUTTON1_PRESSED = BUTTON1_CLICKED = BUTTON4_PRESSED = BUTTON5_PRESSED = 0

    def has_colors(self):
        return True

    def init_pair(self, i, fg, bg):
        pass

    def color_pair(self, i):
        return 256 * i

    def __getattr__(self, name):
        return lambda *a: None


class ColourTests(unittest.TestCase):
    def test_blue_with_and_without_colour(self):
        for env, want in (({}, True), ({"NO_COLOR": "1"}, False)):
            with mock.patch.dict(os.environ, env):
                if not env:
                    os.environ.pop("NO_COLOR", None)
                s = ab.DashScreen(mock.Mock(), FakeCurses(), lambda: None, 5, ab.RexStatus(False), False)
                s.setup()
            self.assertEqual(s.attr("blue") > 0, want)


class RexTests(CommandCase):
    def dash(self, frames):
        it = iter(frames)

        def collect(cfg):
            try:
                rows = next(it)
            except StopIteration:
                raise KeyboardInterrupt
            return {"generated_at": 0, "sandboxes_known": rows is not None, "profiles": rows or []}
        with mock.patch.object(ab, "collect_dashboard", collect):
            return records(self.run_cmd(ab.cmd_dashboard, self.cfg,
                                        argparse.Namespace(watch=True, json=False, interval=2.0))[1])

    def r(self, j=None, **kw):
        return irow("p", j=j if j is not None else dict(ab.NO_JOB), **kw)

    def test_interactive_states(self):
        self.assertEqual(self.dash([[self.r(age=5)], [self.r(age=5)], [self.r(age=725)], [self.r(age=750)],
                                    [self.r(age=7300)], [self.r(age=None)]])[:-1], [
            {"state": "working", "app": "agentbox", "id": "p", "msg": "interactive"},
            {"state": "idle", "app": "agentbox", "id": "p", "msg": "interactive, idle 12m"},
            {"state": "idle", "app": "agentbox", "id": "p", "msg": "interactive, idle 2h01m"},
            {"state": "idle", "app": "agentbox", "id": "p", "msg": "interactive"},
        ])

    def test_never_done_error_or_blocked_while_sessions_run(self):
        frames = [[self.r(j=old_job(), age=a)] for a in (3, 400)] + \
                 [[self.r(j=old_job(exit_code=3), age=a)] for a in (3, 400)] + \
                 [[self.r(j=job("lost", finished_at=None), age=a)] for a in (3, 400)]
        recs = self.dash(frames)[:-1]
        self.assertTrue(recs)
        self.assertEqual({r["state"] for r in recs}, {"working", "idle"})

    def test_a_running_job_still_wins(self):
        recs = self.dash([[self.r(j=job() | {"progress": None}, age=3)]])
        self.assertEqual(recs[0], {"state": "working", "app": "agentbox", "id": "p"})

    def test_falls_back_to_the_job_when_the_sessions_end(self):
        recs = self.dash([[self.r(j=old_job(exit_code=2), age=3)], [self.r(j=old_job(exit_code=2), sessions=0)]])
        self.assertEqual(recs, [{"state": "working", "app": "agentbox", "id": "p", "msg": "interactive"},
                                {"state": "error", "app": "agentbox", "id": "p", "msg": "exit 2"},
                                {"state": "clear", "id": "p"}])

    def test_cleared_when_the_sessions_end_without_a_job(self):
        recs = self.dash([[self.r(age=3)], [self.r(vm="stopped")]])
        self.assertEqual(recs, [{"state": "working", "app": "agentbox", "id": "p", "msg": "interactive"},
                                {"state": "clear", "id": "p"}])


class CollectTests(AgentboxTestCase):
    CFG = '[profiles.a]\nworkspace = "{ws}"\n[profiles.b]\nworkspace = "{ws}"\n'

    def setUp(self):
        super().setUp()
        self.cfg = self.write_config(self.CFG.format(ws=self.ws))
        self.calls = []

    def stub(self, states, out=SAMPLE + GOOD, rc=0):
        def vm_exec(prof, script, stdin=None):
            self.calls.append((prof.name, script))
            return mock.Mock(returncode=rc, stdout=out)
        for p in (mock.patch.object(ab, "sandbox_states", lambda cfg: states),
                  mock.patch.object(ab, "vm_exec", vm_exec),
                  mock.patch.object(ab, "job_info", lambda prof, states=None: dict(ab.NO_JOB))):
            p.start()
            self.addCleanup(p.stop)

    def test_rows_carry_the_sessions_and_a_stopped_vm_is_never_execd(self):
        self.stub({self.cfg.profile("a").sandbox: "running", self.cfg.profile("b").sandbox: "stopped"})
        rows = {r["name"]: r for r in ab.collect_dashboard(self.cfg)["profiles"]}
        self.assertEqual([c[0] for c in self.calls], ["a"])
        self.assertEqual(len(rows["a"]["sessions"]), 3)
        self.assertEqual((rows["a"]["activity_age_s"], rows["a"]["permission_mode"]), (9, "auto"))
        self.assertEqual((rows["b"]["sessions"], rows["b"]["activity_age_s"], rows["b"]["permission_mode"]),
                         ([], None, None))
        self.assertIn("claude_activity", self.calls[0][1])           # one exec: stats and sessions together

    def test_failed_exec_means_no_sessions(self):
        self.stub({self.cfg.profile("a").sandbox: "running"}, rc=1)
        row_ = ab.collect_dashboard(self.cfg)["profiles"][0]
        self.assertEqual((row_["resources"], row_["sessions"]), (None, []))

    def test_dashboard_json_has_the_fields(self):
        self.stub({self.cfg.profile("a").sandbox: "running"})
        code, out, err = self.run_main("dashboard", "--json")
        self.assertEqual(code, 0, err)
        a = json.loads(out)["profiles"][0]
        self.assertEqual(a["sessions"][0], {"agent": "claude", "age_s": 10735})
        self.assertEqual((a["activity_age_s"], a["permission_mode"]), (9, "auto"))


class StatusJsonTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')

    def status(self, vm):
        fake = FakeSbx(exists=True, vm=vm, names=("ab-p",), exec_out={"claude_activity": (0, GOOD)}).install(self)
        code, out, err = self.run_main("status", "--json")
        self.assertEqual(code, 0, err)
        return json.loads(out)["profiles"][0], fake

    def test_running_vm_has_the_fields(self):
        p, fake = self.status("running")
        self.assertEqual(len(p["sessions"]), 3)
        self.assertEqual((p["activity_age_s"], p["permission_mode"]), (9, "auto"))

    def test_stopped_vm_has_none_and_is_never_booted(self):
        p, fake = self.status("stopped")                 # FakeSbx fails the test on an exec into a stopped VM
        self.assertFalse({"sessions", "activity_age_s", "permission_mode"} & set(p))
        self.assertFalse([c for c in fake.calls if c[0] == "exec"])


if __name__ == "__main__":
    unittest.main()
