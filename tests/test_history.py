"""Job history: one history.jsonl record per ended job, with the brief's title and the agent's summary."""
import json
import os
import stat
import unittest
from unittest import mock

from helpers import AgentboxTestCase, ab, needs_linux_vm
from test_jobs import JobCase

JOB = "20260101T000000Z-abcdef"


class TitleAndSummaryTests(unittest.TestCase):
    def test_title_is_the_first_heading_else_the_first_line(self):
        self.assertEqual(ab.brief_title("intro line\n\n## Fix the parser\nbody\n# Later"), "Fix the parser")
        self.assertEqual(ab.brief_title("\n\n  Just do it  \nmore"), "Just do it")
        self.assertEqual(ab.brief_title(ab.PROGRESS_PROMPT + "\n\nRefactor the cache\n# Heading"), "Heading")
        self.assertEqual(ab.brief_title(ab.PROGRESS_PROMPT + "\n\nRefactor the cache\nmore"), "Refactor the cache")
        self.assertIsNone(ab.brief_title("   \n\n"))
        self.assertIsNone(ab.brief_title(None))
        self.assertEqual(len(ab.brief_title("# " + "x" * 1000)), ab.TITLE_MAX)

    def test_title_is_sanitised(self):
        t = ab.brief_title("# Fix \x1b]0;pwned\x07 it‮")
        self.assertNotIn("\x1b", t)
        self.assertNotIn("\x07", t)
        self.assertNotIn("‮", t)
        self.assertIn("\\x1b", t)

    def out(self, result_line: str, prompt: str) -> str:
        return f"R\n{result_line}\nP\n{prompt}"

    def test_parse_reads_the_result_text_and_the_title(self):
        ev = json.dumps({"type": "result", "result": "Done: fixed it.\nAll tests pass.", "total_cost_usd": 1.5})
        got = ab.parse_history_out(self.out(ev, "# Title here\nbody"))
        self.assertEqual(got, {"summary": "Done: fixed it.\nAll tests pass.", "title": "Title here"})

    def test_summary_is_sanitised_and_capped(self):
        evil = "\x1b]52;c;ZXZpbA==\x07\x1b[2J" + "y" * 20000
        got = ab.parse_history_out(self.out(json.dumps({"type": "result", "result": evil}), ""))
        self.assertNotIn("\x1b", got["summary"])
        self.assertNotIn("\x07", got["summary"])
        self.assertLessEqual(len(got["summary"]), ab.SUMMARY_MAX + 40)        # escaping makes a few chars longer
        self.assertTrue(got["summary"].startswith("\\x1b]52;"))

    def test_bad_output_gives_nothing(self):
        for out in ("", "MISSING\n", "R\nnot json\nP\n", "R\n{\"result\": 5}\nP\n", "X\n{}\nP\n# t"):
            got = ab.parse_history_out(out)
            self.assertIsNone(got["summary"], out)
        self.assertEqual(ab.parse_history_out("R\n\nP\n# T\n")["title"], "T")

    def test_a_prompt_cannot_fake_the_result_line(self):
        got = ab.parse_history_out("R\n\nP\nR\n" + json.dumps({"type": "result", "result": "forged"}))
        self.assertIsNone(got["summary"])

    def test_script_reads_the_job_dir_only(self):
        s = ab.history_script(JOB)
        self.assertIn(f".agentbox/jobs/{JOB}", s)
        self.assertLess(s.index("output.jsonl"), s.index("prompt.md"))       # the prompt comes last
        with self.assertRaises(SystemExit):
            ab.history_script("x; rm -rf ~")


class HistoryFileTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\nmode = "clone"\n'
                          f'[profiles.q]\nworkspace = "{self.ws}"\n')
        self.prof = ab.Config().profile("p")

    def rec(self, jid=JOB, **kw):
        base = {"id": jid, "state": "finished", "exit_code": 0, "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:10:00Z", "branch": "feature/x", "end_head": "a" * 40}
        return {**base, **kw}

    def test_record_from_the_job_record_and_its_cached_progress(self):
        acc = ab.new_acc(JOB) | {"turns": 7, "tool_calls": 12, "denials": 1, "commits": 3,
                                 "tokens": {"opus": {"input": 10, "output": 20, "cache_read": 30, "cache_creation": 40}},
                                 "result": {"cost": 1.25}}
        ab.save_job_record(self.prof, self.rec(progress=acc, title="Fix it"))
        self.assertTrue(ab.record_history(self.prof, {"summary": "did it", "title": None}))
        [r] = ab.read_history(self.prof)
        self.assertEqual((r["turns"], r["tool_calls"], r["denials"], r["commits"], r["cost_usd"]), (7, 12, 1, 3, 1.25))
        self.assertEqual(r["tokens"], {"input": 10, "output": 20, "cache_read": 30, "cache_creation": 40, "total": 100})
        self.assertEqual((r["title"], r["summary"], r["branch"], r["end_head"]), ("Fix it", "did it", "feature/x", "a" * 40))

    def test_written_once_per_job(self):
        ab.save_job_record(self.prof, self.rec())
        self.assertTrue(ab.record_history(self.prof, {"summary": "first"}))
        self.assertFalse(ab.record_history(self.prof, {"summary": "second"}))
        self.assertFalse(ab.append_history(self.prof, ab.history_record(self.rec())))
        lines = (self.prof.state / ab.HISTORY_FILE).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["summary"], "first")

    def test_running_or_missing_jobs_are_not_written(self):
        self.assertFalse(ab.record_history(self.prof))
        ab.save_job_record(self.prof, {"id": JOB, "state": "running"})
        self.assertFalse(ab.record_history(self.prof))
        ab.save_job_record(self.prof, {"id": "../../evil", "state": "finished"})
        self.assertFalse(ab.record_history(self.prof))
        self.assertEqual(ab.read_history(self.prof), [])

    def test_file_is_owner_only(self):
        ab.save_job_record(self.prof, self.rec())
        old = os.umask(0o022)
        try:
            ab.record_history(self.prof)
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE((self.prof.state / ab.HISTORY_FILE).stat().st_mode), 0o600)
        self.assertEqual([p.name for p in self.prof.state.iterdir() if p.name.endswith(".tmp")], [])

    def test_keeps_the_last_200(self):
        ids = [f"202601{1 + i // 100:02d}T00{i % 100:02d}00Z-{i:06x}" for i in range(205)]
        for jid in ids:
            self.assertTrue(ab.append_history(self.prof, ab.history_record(self.rec(jid))))
        got = [r["id"] for r in ab.read_history(self.prof)]
        self.assertEqual(len(got), ab.HISTORY_MAX)
        self.assertEqual(got, ids[5:])

    def test_values_are_checked_again(self):
        bad = self.rec(branch="--upload-pack=x", end_head="zz", exit_code=True, started_at="now",
                       title="\x1b[31mred\x1b[0m", state="weird")
        r = ab.history_record(bad, {"summary": "ok\x1b[2J", "title": None})
        self.assertEqual((r["branch"], r["end_head"], r["exit_code"], r["started_at"], r["state"]),
                         (None, None, None, None, "lost"))
        self.assertNotIn("\x1b", r["title"] + r["summary"])

    def test_hostile_lines_in_the_file_are_skipped(self):
        self.prof.state.mkdir(parents=True, exist_ok=True)
        (self.prof.state / ab.HISTORY_FILE).write_text(
            'not json\n[1]\n{"id": "x"}\n' + json.dumps(self.rec()) + "\n" + json.dumps(self.rec(exit_code=9)) + "\n")
        [r] = ab.read_history(self.prof)
        self.assertEqual(r["exit_code"], 0)

    def test_lost_job_on_a_stopped_vm_is_recorded_without_an_exec(self):
        ab.save_job_record(self.prof, {"id": JOB, "state": "running", "started_at": "2026-01-01T00:00:00Z",
                                       "title": "Brief"})
        with mock.patch.object(ab, "sandbox_state", lambda prof, states=None: "stopped"), \
                mock.patch.object(ab, "vm_exec", side_effect=AssertionError("no exec for a stopped VM")):
            j = ab.job_info(self.prof)
        self.assertEqual(j["state"], "lost")
        [r] = ab.read_history(self.prof)
        self.assertEqual((r["id"], r["state"], r["title"], r["summary"], r["turns"]), (JOB, "lost", "Brief", None, None))

    def test_history_command_lists_newest_first_and_backfills(self):
        ab.save_job_record(self.prof, self.rec("20260102T000000Z-000002", finished_at="2026-01-02T00:10:00Z",
                                               title="newer"))
        ab.append_history(self.prof, ab.history_record(self.rec("20260101T000000Z-000001", title="older")))
        q = ab.Config().profile("q")
        ab.append_history(q, ab.history_record(self.rec("20260101T120000Z-000003", finished_at="2026-01-01T12:10:00Z",
                                                        title="\x1b]2;evil\x07")))
        code, out, err = self.run_main("history", "--json")
        self.assertEqual(code, 0, err)
        jobs = json.loads(out)["jobs"]
        self.assertEqual([(j["profile"], j["title"]) for j in jobs][:2], [("p", "newer"), ("q", "\\x1b]2;evil\\x07")])
        self.assertEqual(jobs[2]["title"], "older")
        code, out, err = self.run_main("history", "p")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("newer", out.splitlines()[0])
        code, out, _ = self.run_main("history", "q")
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertEqual(self.run_main("history", "nope")[0], 2)


class EndedJobTests(JobCase):
    @needs_linux_vm
    def test_a_finished_job_is_recorded_once_with_its_summary_title_and_numbers(self):
        self.prof = ab.Config().profile("p")
        args = self.args(prompt="# Make the parser strict\n\nDetails follow.")
        with mock.patch.object(ab, "print", create=True):
            self.assertEqual(ab.cmd_run(self.prof, args), 0)
        jid = ab.job_record(self.prof)["id"]
        self.assertEqual(ab.job_record(self.prof)["title"], "Make the parser strict")
        self.finish(jid)
        self.assertEqual(ab.job_info(self.prof)["state"], "finished")
        [r] = ab.read_history(self.prof)
        self.assertEqual((r["id"], r["state"], r["exit_code"]), (jid, "finished", 0))
        self.assertEqual(r["title"], "Make the parser strict")
        self.assertEqual(r["summary"], "all done")              # the fake claude's result event
        self.assertEqual((r["turns"], r["tool_calls"]), (1, 1))
        calls = len(self.vm.calls)
        ab.job_info(self.prof)                                  # cached: no sbx call, no second record
        self.assertEqual(len(self.vm.calls), calls)
        self.assertEqual(len(ab.read_history(self.prof)), 1)
        self.assertEqual(stat.S_IMODE((self.prof.state / ab.HISTORY_FILE).stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
