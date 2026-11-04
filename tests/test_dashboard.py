import json
import time
import unittest
from unittest import mock

from helpers import REAL_RUN, AgentboxTestCase, ab, real_sh, needs_linux_vm

SAMPLE = """stat1 cpu 100 0 100 800 0 0 0 0 0 0
stat2 cpu 150 0 150 900 0 0 0 0 0 0
uptime 3725.41
load 0.52 0.40 0.30
cpus 4
mem MemTotal: 8000000 kB
mem MemAvailable: 6000000 kB
disk /dev/vda 10000000 2500000 7500000 25% /
"""

VIEW = {"elapsed_s": 724, "last_event_age_s": 6, "stalled": False, "turns": 88, "tool_calls": 61, "tools_top": [],
        "tokens": {"input": 912, "output": 41200, "cache_read": 3811000, "cache_creation": 210000, "total": 4063112},
        "cost_usd": 1.84, "cost_is_estimate": True, "commits": 1, "expect_commits": None, "plan": None,
        "note": None, "stale": False}


class ParseTests(unittest.TestCase):
    def test_parses_the_probe_output(self):
        r = ab.parse_vm_stats(SAMPLE)
        self.assertEqual((r["uptime_s"], r["cpus"], r["load"]), (3725, 4, [0.52, 0.40, 0.30]))
        self.assertEqual(r["cpu_pct"], 50.0)            # 100 busy of 200 jiffies
        self.assertEqual((r["mem_total"], r["mem_used"]), (8000000 * 1024, 2000000 * 1024))
        self.assertEqual((r["disk_total"], r["disk_used"]), (10000000 * 1024, 2500000 * 1024))

    def test_garbage_is_none(self):
        for bad in ("", "hello\nworld", SAMPLE.replace("cpus 4", "cpus four"), SAMPLE.replace("stat2", "x")):
            self.assertIsNone(ab.parse_vm_stats(bad), bad)

    @needs_linux_vm
    def test_the_real_script_parses(self):
        with real_sh():
            r = REAL_RUN(["sh", "-c", ab.VM_STATS_SCRIPT], capture_output=True, text=True)
        self.assertIsNotNone(ab.parse_vm_stats(r.stdout), r.stdout + r.stderr)


class RenderTests(unittest.TestCase):
    def data(self, **row):
        base = {"name": "demo", "enabled": True, "sandbox": "s", "agent": "claude", "mode": "clone", "vm": "running",
                "resources": ab.parse_vm_stats(SAMPLE),
                "job": {"id": "j1", "state": "running", "progress": VIEW}}
        return {"generated_at": int(time.time()), "sandboxes_known": True,
                "profiles": [base | row, {"name": "old", "enabled": True, "vm": "stopped"}]}

    def test_card_shows_the_stats_and_lists_stopped_ones(self):
        text = "\n".join(ab.dashboard_lines(self.data(), ab.Style(False)))
        for want in ("1 running", "demo", "uptime 1h02m", "cpu", "50%", "memory", "disk", "idle 6s", "elapsed 12m",
                     "tokens 4.1M", "~$1.84", "not running: old (stopped)"):
            self.assertIn(want, text)

    def test_box_lines_are_equally_wide(self):
        lines = ab.dashboard_lines(self.data(), ab.Style(False), 78)
        self.assertEqual({len(ln) for ln in lines if ln and ln[0] in "╭│╰"}, {78})

    def test_no_escape_codes_without_colour_and_vm_text_is_sanitised(self):
        text = "\n".join(ab.dashboard_lines(self.data(job={"id": "j\x1b[2J", "state": "lost", "progress": None}),
                                           ab.Style(False)))
        self.assertNotIn("\x1b", text)

    def test_unreadable_vm_and_no_job(self):
        text = "\n".join(ab.dashboard_lines(self.data(resources=None, job=None), ab.Style(False)))
        self.assertIn("could not be read", text)
        self.assertIn("no job", text)

    def test_nothing_running(self):
        d = {"generated_at": 0, "sandboxes_known": True, "profiles": []}
        self.assertIn("no sandbox is running", "\n".join(ab.dashboard_lines(d, ab.Style(False))))


class CommandTests(AgentboxTestCase):
    CFG = """
        [profiles.a]
        workspace = "{ws}"
        [profiles.b]
        workspace = "{ws}"
        [profiles.c]
        workspace = "{ws}"
        enabled = false
    """

    def setUp(self):
        super().setUp()
        self.write_config(self.CFG.format(ws=self.ws))

    def stub(self, states, stats=SAMPLE):
        calls = []

        def vm_exec(prof, script, stdin=None):
            calls.append(prof.name)
            return mock.Mock(returncode=0, stdout=stats)
        patches = [mock.patch.object(ab, "sandbox_states", lambda cfg: states),
                   mock.patch.object(ab, "vm_exec", vm_exec),
                   mock.patch.object(ab, "job_info", lambda prof, states=None: dict(ab.NO_JOB))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_only_running_sandboxes_are_probed(self):
        calls = self.stub({ab.Config().profile("a").sandbox: "running", ab.Config().profile("b").sandbox: "stopped"})
        data = ab.collect_dashboard(ab.Config())
        self.assertEqual(calls, ["a"])
        vms = {r["name"]: r["vm"] for r in data["profiles"]}
        self.assertEqual(vms, {"a": "running", "b": "stopped", "c": "disabled"})
        self.assertEqual(data["profiles"][0]["resources"]["cpus"], 4)

    def test_json_is_one_document(self):
        self.stub({})
        code, out, err = self.run_main("dashboard", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["profiles"]), 3)

    def test_failed_listing_is_reported(self):
        self.stub(None)
        code, out, _ = self.run_main("dashboard")
        self.assertEqual(code, 0)
        self.assertIn("could not list sandboxes", out)

    def test_bad_flags(self):
        self.assertEqual(self.run_main("dashboard", "--json", "--watch")[0], 2)
        self.assertEqual(self.run_main("dashboard", "--interval", "1")[0], 2)


if __name__ == "__main__":
    unittest.main()
