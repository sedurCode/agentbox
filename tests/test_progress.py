import argparse
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from helpers import REAL_RUN, AgentboxTestCase, ab, real_sh
from test_jobs import JobCase

JOBPROGRESS = ab.SHARE_DIR / "jobprogress"


def iso(ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - ago)) + ".123Z"


def asst(mid, model="claude-sonnet-5-5", usage=None, tools=(), ago=1, text=None):
    content = [{"type": "tool_use", "id": f"tu_{mid}_{n}", "name": n, "input": {}} for n in tools]
    if text:
        content.append({"type": "text", "text": text})
    msg = {"id": mid, "model": model, "content": content}
    if usage is not None:
        msg["usage"] = usage
    return json.dumps({"type": "assistant", "timestamp": iso(ago), "message": msg})


USAGE = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}


def sh(*argv, env=None, cwd=None):
    with real_sh():
        return REAL_RUN(["sh", *map(str, argv)], env=env, cwd=cwd, capture_output=True, text=True)


class IngestTests(unittest.TestCase):
    def ingest(self, *lines):
        acc = ab.new_acc("j")
        ab.ingest(acc, "\n".join(lines) + "\n")
        return acc

    def test_usage_counted_once_per_message_id(self):
        # stream-json repeats the message id (and its usage) for every content block
        acc = self.ingest(asst("m1", usage=USAGE, tools=["Bash"]), asst("m1", usage=USAGE, tools=["Read"]),
                          asst("m1", usage=USAGE, text="x"), asst("m2", usage=USAGE))
        self.assertEqual(acc["turns"], 2)
        self.assertEqual(acc["tokens"]["sonnet"], {"input": 20, "output": 10, "cache_read": 200, "cache_creation": 40})
        self.assertEqual((acc["tool_calls"], acc["tools"]), (2, {"Bash": 1, "Read": 1}))

    def test_the_same_tool_use_block_is_not_counted_twice(self):
        acc = self.ingest(asst("m1", tools=["Bash"]), asst("m1", tools=["Bash"]))
        self.assertEqual(acc["tool_calls"], 1)

    def test_dedup_survives_incremental_chunks(self):
        acc = ab.new_acc("j")
        ab.ingest(acc, asst("m1", usage=USAGE) + "\n")
        ab.ingest(acc, asst("m1", usage=USAGE) + "\n" + asst("m2", usage=USAGE) + "\n")
        self.assertEqual((acc["turns"], acc["tokens"]["sonnet"]["input"]), (2, 20))

    def test_garbage_is_ignored(self):
        acc = self.ingest("not json", "[1,2]", '{"type":"assistant","message":"x"}', "{" * 100000,
                          '{"type":"assistant","message":{"id":"m","usage":{"input_tokens":-5,"output_tokens":"9",'
                          '"cache_read_input_tokens":true,"cache_creation_input_tokens":1e99}}}', "")
        self.assertEqual(acc["turns"], 1)
        self.assertEqual(sum(sum(r.values()) for r in acc["tokens"].values()), 0)

    def test_tool_names_are_capped(self):
        names = [f"t{i}" for i in range(ab.MAX_TOOL_NAMES + 20)]
        acc = self.ingest(*[asst(f"m{i}", tools=[n]) for i, n in enumerate(names)])
        self.assertEqual(len(acc["tools"]), ab.MAX_TOOL_NAMES + 1)
        self.assertEqual(acc["tools"]["(other)"], 20)

    def test_last_event_time(self):
        acc = self.ingest(asst("a", ago=50), asst("b", ago=5), asst("c", ago=30))
        self.assertAlmostEqual(time.time() - acc["last_ts"], 5, delta=3)
        self.assertIsNone(ab.event_time("yesterday"))
        self.assertIsNone(ab.event_time(True))
        self.assertEqual(ab.event_time(1_700_000_000_000), 1_700_000_000.0)


class CostTests(unittest.TestCase):
    def test_estimate_from_the_price_table(self):
        acc = ab.new_acc("j")
        ab.ingest(acc, asst("m1", usage={"input_tokens": 1_000_000, "output_tokens": 100_000}) + "\n")
        self.assertAlmostEqual(ab.estimate_cost(acc), 3.0)                    # sonnet 2 + 100k * 10/M

    def test_unknown_model_leaves_the_cost_out(self):
        acc = ab.new_acc("j")
        ab.ingest(acc, asst("m1", model="gpt-9", usage=USAGE) + "\n" + asst("m2", usage=USAGE) + "\n")
        self.assertIsNone(ab.estimate_cost(acc))
        self.assertIsNone(ab.estimate_cost(ab.new_acc("j")))                  # nothing counted yet

    def test_every_family_has_a_price(self):
        self.assertEqual(set(ab.PRICES), {"opus", "sonnet", "haiku", "fable"})
        self.assertEqual(ab.model_family("claude-OPUS-5-5"), "opus")
        self.assertIsNone(ab.model_family(None))


class PlanParseTests(unittest.TestCase):
    def test_parse(self):
        steps, note = ab.parse_plan(["pending\tone", "active\ttwo", "done\tthree", "note\tfirst", "note\tlast", "x\ty",
                                     "no tab", "done\t"])
        self.assertEqual([s["status"] for s in steps], ["pending", "active", "done", "done"])
        self.assertEqual(note, "last")

    def test_caps_and_control_characters(self):
        lines = [f"pending\tstep {i}" for i in range(80)] + ["note\t" + "n" * 1000]
        steps, note = ab.parse_plan(lines)
        self.assertEqual(len(steps), ab.MAX_PLAN_STEPS)
        self.assertEqual(len(note), ab.MAX_NOTE_CHARS)
        steps, note = ab.parse_plan(["pending\t\x1b]52;c;ZXZpbA==\x07evil‮", "note\t\x1b[2Jboom\r"])
        self.assertNotIn("\x1b", steps[0]["text"] + note)
        self.assertNotIn("\x07", steps[0]["text"])
        self.assertNotIn("‮", steps[0]["text"])
        self.assertIn("\\x1b", note)


class JobProgressScriptTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.jd = self.tmp / "jd"
        self.jd.mkdir()
        self.env = {**os.environ, "AGENTBOX_JOB_DIR": str(self.jd)}

    def jp(self, *args, env=None):
        return sh(JOBPROGRESS, *args, env=env or self.env)

    def tsv(self):
        return (self.jd / "progress.tsv").read_text()

    def test_plan_start_done_note(self):
        self.assertEqual(self.jp("plan", "one", 'two "quoted" \\ back').returncode, 0)
        self.assertEqual(self.tsv(), 'pending\tone\npending\ttwo "quoted" \\ back\n')
        self.assertEqual(self.jp("start", "2").returncode, 0)
        self.assertEqual(self.jp("note", "halfway there").returncode, 0)
        self.assertEqual(self.jp("done", "1").returncode, 0)
        self.assertEqual(self.tsv(), 'done\tone\nactive\ttwo "quoted" \\ back\nnote\thalfway there\n')
        self.assertEqual(self.jp("note", "").returncode, 0)                        # clears it
        self.assertNotIn("note", self.tsv())
        self.jp("note", "kept")
        self.jp("plan", "new plan")                                                # a new plan keeps the note
        self.assertEqual(self.tsv(), "pending\tnew plan\nnote\tkept\n")
        steps, note = ab.parse_plan(self.tsv().splitlines())
        self.assertEqual((len(steps), note), (1, "kept"))

    def test_validation(self):
        self.assertEqual(self.jp("start", "1").returncode, 2)                      # no plan yet
        self.jp("plan", "a", "b")
        before = self.tsv()
        bad = [("plan",), ("plan", ""), ("plan", *["s"] * 51), ("plan", "x" * 201), ("plan", "tab\there"),
               ("plan", "line\nbreak"), ("plan", "esc\x1b[2J"), ("start",), ("start", "0"), ("start", "3"),
               ("start", "-1"), ("start", "1x"), ("start", ""), ("start", "01"), ("done", "9"), ("done", "1", "2"),
               ("note", "x" * 301), ("note", "a\tb"), ("note",), ("frobnicate",), ()]
        for argv in bad:
            r = self.jp(*argv)
            self.assertEqual(r.returncode, 2, (argv, r.stderr))
            self.assertTrue(r.stderr.startswith("jobprogress:"), argv)
        self.assertEqual(self.tsv(), before)                                       # nothing half-applied
        self.assertEqual(self.jp("plan", *["s"] * 50).returncode, 0)               # the limits themselves are fine
        self.assertEqual(self.jp("plan", "x" * 200).returncode, 0)
        self.assertEqual(self.jp("note", "n" * 300).returncode, 0)

    def test_needs_a_job_dir(self):
        env = {k: v for k, v in self.env.items() if k != "AGENTBOX_JOB_DIR"}
        self.assertEqual(self.jp("plan", "a", env=env).returncode, 2)
        self.assertEqual(self.jp("plan", "a", env={**env, "AGENTBOX_JOB_DIR": str(self.jd / "nope")}).returncode, 2)

    def test_write_is_atomic_and_leaves_nothing_behind(self):
        self.assertIn('mv "$tmp" "$f"', JOBPROGRESS.read_text())                   # temp file + rename
        self.jp("plan", "a", "b")
        self.jp("start", "1")
        self.assertEqual(sorted(p.name for p in self.jd.iterdir()), ["progress.tsv"])   # no tmp, no lock

    def test_failed_command_does_not_touch_the_file(self):
        self.jp("plan", "a")
        (self.jd / "progress.tsv").chmod(0o444)
        before = self.tsv()
        self.jp("note", "x" * 301)
        self.assertEqual(self.tsv(), before)

    def test_a_stale_lock_does_not_block_forever(self):
        (self.jd / "progress.lock").mkdir()
        with mock.patch.dict(os.environ):
            r = self.jp("plan", "a")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_install_sh_installs_every_share_file(self):
        script = (ab.SHARE_DIR.parent / "install.sh").read_text()
        for f in (p for p in ab.SHARE_DIR.iterdir() if p.is_file()):
            self.assertIn(f'share/{f.name}"', script, f"install.sh does not copy share/{f.name}")

    def test_wrapper_puts_the_job_dir_at_the_end_of_path(self):
        (self.jd / "prompt.md").write_text("p")
        for n in ("wrap.sh", "jobprogress"):
            (self.jd / n).write_text((ab.SHARE_DIR / ("jobwrap" if n == "wrap.sh" else n)).read_text())
        env = {k: v for k, v in self.env.items() if k != "AGENTBOX_JOB_DIR"}
        r = sh(self.jd / "wrap.sh", self.jd, self.tmp, "sh", "-c", 'echo "$AGENTBOX_JOB_DIR"; echo "$PATH"; jobprogress plan x',
               env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = (self.jd / "output.jsonl").read_text().splitlines()
        self.assertEqual(out[0], str(self.jd))
        self.assertTrue(out[1].endswith(":" + str(self.jd)))
        self.assertEqual(self.tsv(), "pending\tx\n")


class ProgressCase(JobCase):
    def setUp(self):
        super().setUp()
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "30"

    def run_job(self, **kw):
        job = self.start(**kw)
        self.job = job
        return job

    def set_output(self, job, *lines):
        (self.vm.jobdir(job) / "output.jsonl").write_text("".join(ln + "\n" for ln in lines))

    def progress(self, *argv, expect=0):
        code, out, err = self.run_main("progress", "p", *argv)
        self.assertEqual(code, expect, err)
        return out

    def view(self, *argv):
        return json.loads(self.progress("--json", *argv))

    def execs(self):
        return [c for c in self.vm.calls if c[0] == "exec"]

    def scripts_since(self, n):
        return [c[-1] for c in self.vm.calls[n:] if c[0] == "exec" and "SIZE" in c[-1]]


class SignalTests(ProgressCase):
    def test_turns_tools_tokens_and_the_estimate(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE, tools=["Bash", "Read"]), asst("m1", usage=USAGE, tools=["Bash"]),
                        asst("m2", usage=USAGE, tools=["Bash"]))
        v = self.view()
        self.assertEqual((v["turns"], v["tool_calls"]), (2, 3))
        self.assertEqual(v["tools_top"], [{"name": "Bash", "count": 2}, {"name": "Read", "count": 1}])
        self.assertEqual(v["tokens"], {"input": 20, "output": 10, "cache_read": 200, "cache_creation": 40, "total": 270})
        self.assertTrue(v["cost_is_estimate"])
        self.assertGreater(v["cost_usd"], 0)
        self.assertIn("~$", self.progress())
        self.assertIn("(estimate)", self.progress())

    def test_a_model_without_a_price_has_no_cost(self):
        job = self.run_job()
        self.set_output(job, asst("m1", model="mystery-1", usage=USAGE))
        v = self.view()
        self.assertEqual((v["cost_usd"], v["cost_is_estimate"]), (None, False))
        self.assertNotIn("cost", self.progress())

    def test_the_result_cost_wins_once_finished(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "0"
        job = self.run_job()
        self.finish(job)
        self.set_output(job, asst("m1", usage={"input_tokens": 1_000_000}),
                        json.dumps({"type": "result", "total_cost_usd": 4.26, "num_turns": 9}))
        v = self.view()
        self.assertEqual((v["cost_usd"], v["cost_is_estimate"]), (4.26, False))
        out = self.progress()
        self.assertIn("$4.26", out)
        self.assertNotIn("~", out)

    def test_stall_detection(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE, ago=400))
        v = self.view()
        self.assertTrue(v["stalled"])
        self.assertAlmostEqual(v["last_event_age_s"], 400, delta=5)
        self.assertIn("STALLED", self.progress())
        self.set_output(job, asst("m1", usage=USAGE, ago=400), asst("m2", usage=USAGE, ago=20))
        v = self.view()
        self.assertFalse(v["stalled"])
        self.assertNotIn("STALLED", self.progress())

    def test_stall_after_is_a_profile_key(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE, ago=60))
        self.assertFalse(self.view()["stalled"])
        (self.cfg_dir / "agentbox.toml").write_text(
            (self.cfg_dir / "agentbox.toml").read_text().replace("[profiles.p]\n", "[profiles.p]\nstall_after = 30\n", 1))
        self.assertTrue(self.view()["stalled"])
        (self.cfg_dir / "agentbox.toml").write_text(
            (self.cfg_dir / "agentbox.toml").read_text().replace("stall_after = 30", "stall_after = 0"))
        code, _, err = self.run_main("progress", "p")
        self.assertEqual(code, 2)

    def test_a_finished_job_is_never_stalled(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "0"
        job = self.run_job()
        self.finish(job)
        self.set_output(job, asst("m1", usage=USAGE, ago=4000))
        v = self.view()
        self.assertEqual((v["stalled"], v["last_event_age_s"]), (False, None))

    def git(self, *a):
        with real_sh():
            return REAL_RUN(["git", "-C", str(self.ws), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                            capture_output=True, text=True, check=True)

    def test_commits_since_the_job_started(self):
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "before")
        job = self.run_job(expect_commits=3)
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual((self.vm.jobdir(job) / "start_head").read_text().strip(), head)
        self.assertEqual(self.view()["commits"], 0)
        self.git("commit", "-q", "--allow-empty", "-m", "one")
        self.git("commit", "-q", "--allow-empty", "-m", "two")
        v = self.view()
        self.assertEqual((v["commits"], v["expect_commits"]), (2, 3))
        self.assertIn("commits 2/3", self.progress())

    def test_commits_unknown_without_git_or_with_a_forged_start_head(self):
        job = self.run_job()                                          # the workspace is not a git repo
        self.assertIsNone(self.view()["commits"])
        for forged in ("--output=x", "HEAD; touch pwned", "a" * 41, "A" * 40):
            (self.vm.jobdir(job) / "start_head").write_text(forged)
            self.assertIsNone(self.view()["commits"], forged)
        self.assertFalse((self.vm.home / "pwned").exists())

    def test_expect_commits_is_validated(self):
        for n in (0, -1, 10001):
            with self.assertRaises(SystemExit):
                ab.cmd_run(self.prof, self.args(expect_commits=n))
        self.assertIsNone(self.view_after_start()["expect_commits"])

    def view_after_start(self):
        self.run_job()
        return self.view()


class RunInstallTests(ProgressCase):
    def test_reporter_and_paragraph_are_installed_by_default(self):
        job = self.run_job()
        d = self.vm.jobdir(job)
        self.assertTrue((d / "jobprogress").read_text().startswith("#!/bin/sh"))
        self.assertEqual(oct((d / "jobprogress").stat().st_mode & 0o777), "0o700")
        prompt = (d / "prompt.md").read_text()
        self.assertTrue(prompt.startswith("Progress reporting: before you start"))
        self.assertTrue(prompt.endswith("\n\n" + "SECRET-PROMPT-TEXT summarise the repo"))
        for call in self.vm.calls:                                    # stdin only, never argv
            self.assertNotIn("Progress reporting", " ".join(map(str, call)))

    def test_no_progress_changes_nothing(self):
        job = self.run_job(no_progress=True)
        d = self.vm.jobdir(job)
        self.assertFalse((d / "jobprogress").exists())
        self.assertEqual((d / "prompt.md").read_text(), "SECRET-PROMPT-TEXT summarise the repo")


class AgentPlanTests(ProgressCase):
    def test_plan_bar_and_note(self):
        job = self.run_job()
        d = self.vm.jobdir(job)
        (d / "progress.tsv").write_text("done\ta\ndone\tb\nactive\tc\npending\td\nnote\tcompiling\n")
        v = self.view()
        self.assertEqual((v["plan"]["done"], v["plan"]["total"], v["plan"]["current"]), (2, 4, 3))
        self.assertEqual(v["note"], "compiling")
        out = self.progress()
        self.assertIn("[##########----------] 2/4", out)
        self.assertIn("now: step 3: c", out)
        self.assertIn("agent's plan", out)
        self.assertIn("note: compiling", out)

    def test_no_plan_means_objective_lines_only(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE))
        v = self.view()
        self.assertEqual((v["plan"], v["note"]), (None, None))
        out = self.progress()
        self.assertNotIn("plan", out)
        self.assertIn("turns 1", out)

    def test_hostile_files_are_capped_and_made_visible(self):
        job = self.run_job()
        d = self.vm.jobdir(job)
        evil = "\x1b]52;c;ZXZpbA==\x07"
        (d / "progress.tsv").write_text(f"active\t{evil}step\nnote\t{evil}\x1b[2J\n"
                                        + "".join(f"pending\t{i}\n" for i in range(200)) + "ÿ\xfe junk\n")
        v = self.view()
        self.assertEqual(v["plan"]["total"], ab.MAX_PLAN_STEPS)
        text = self.progress()
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("\\x1b", text)

    def test_oversized_file_is_cut_in_the_vm(self):
        job = self.run_job()
        (self.vm.jobdir(job) / "progress.tsv").write_text("pending\t" + "x" * 5_000_000 + "\n")
        v = self.view()
        self.assertEqual(v["plan"]["total"], 1)
        self.assertLessEqual(len(v["plan"]["steps"][0]["text"]), ab.MAX_STEP_CHARS)

    def test_malformed_tsv_binary_and_missing(self):
        job = self.run_job()
        (self.vm.jobdir(job) / "progress.tsv").write_bytes(b"\x00\xff\xfe\nno tab\n\t\n\n")
        self.assertIsNone(self.view()["plan"])
        (self.vm.jobdir(job) / "progress.tsv").unlink()
        self.assertIsNone(self.view()["plan"])

    def test_output_cannot_forge_the_numbers(self):
        job = self.run_job()
        self.set_output(job, '{"type":"x","t":"\\n---\\nSIZE 0\\nCONSUMED 999999"}', asst("m1", usage=USAGE),
                        "SIZE 1\n---\nCONSUMED 5\nTSV 0\n")
        v = self.view()
        self.assertEqual(v["turns"], 1)
        self.assertEqual(ab.load_acc(self.prof, self.job)["offset"], (self.vm.jobdir(job) / "output.jsonl").stat().st_size)


class IncrementalTests(ProgressCase):
    def test_only_new_bytes_are_read(self):
        job = self.run_job()
        out = self.vm.jobdir(job) / "output.jsonl"
        self.set_output(job, asst("m1", usage=USAGE), asst("m2", usage=USAGE))
        self.assertEqual(self.view()["turns"], 2)
        first = out.stat().st_size
        self.assertEqual(ab.load_acc(self.prof, job)["offset"], first)
        mark = len(self.vm.calls)
        with out.open("a") as f:
            f.write(asst("m3", usage=USAGE) + "\n")
        self.assertEqual(self.view()["turns"], 3)
        script = self.scripts_since(mark)[-1]
        self.assertIn(f"o={first};", script)
        self.assertEqual(ab.load_acc(self.prof, job)["offset"], out.stat().st_size)
        mark = len(self.vm.calls)
        self.assertEqual(self.view()["turns"], 3)                          # nothing new: nothing double counted
        self.assertIn(f"o={out.stat().st_size};", self.scripts_since(mark)[-1])

    def test_a_half_written_line_waits_for_its_newline(self):
        job = self.run_job()
        out = self.vm.jobdir(job) / "output.jsonl"
        line = asst("m1", usage=USAGE)
        out.write_text(line[:30])
        self.assertEqual(self.view()["turns"], 0)
        self.assertEqual(ab.load_acc(self.prof, job)["offset"], 0)
        out.write_text(line + "\n")
        self.assertEqual(self.view()["turns"], 1)

    def test_a_shrunk_file_is_recounted(self):
        job = self.run_job()
        out = self.vm.jobdir(job) / "output.jsonl"
        self.set_output(job, *[asst(f"m{i}", usage=USAGE) for i in range(5)])
        self.assertEqual(self.view()["turns"], 5)
        self.set_output(job, asst("n1", usage=USAGE))                        # the agent rewrote it, smaller
        v = self.view()
        self.assertEqual((v["turns"], v["tokens"]["input"]), (1, 10))
        self.assertEqual(ab.load_acc(self.prof, job)["offset"], out.stat().st_size)

    def test_a_corrupt_or_foreign_cache_is_ignored(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE))
        for junk in ("not json", json.dumps({"job": job, "offset": "x"}), json.dumps(ab.new_acc("other-job")), "[]"):
            self.prof.state.mkdir(parents=True, exist_ok=True)
            (self.prof.state / ab.PROGRESS_CACHE).write_text(junk)
            self.assertEqual(self.view()["turns"], 1, junk)

    def test_big_files_are_read_in_rounds(self):
        job = self.run_job()
        self.set_output(job, *[asst(f"m{i}", usage=USAGE) for i in range(40)])
        size = (self.vm.jobdir(job) / "output.jsonl").stat().st_size
        with mock.patch.object(ab, "PROGRESS_CHUNK", size // 4):
            self.assertEqual(self.view()["turns"], 40)


class NoExecTests(ProgressCase):
    def finished_job(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "0"
        job = self.run_job()
        self.finish(job)
        self.set_output(job, asst("m1", usage=USAGE, tools=["Bash"]),
                        json.dumps({"type": "result", "total_cost_usd": 1.5}))
        return job

    def test_finished_job_is_cached_and_then_needs_no_sbx_call(self):
        job = self.finished_job()
        self.assertEqual(self.view()["cost_usd"], 1.5)                       # first look: one read, then cached
        rec = ab.job_record(self.prof)
        self.assertEqual((rec["progress"]["turns"], rec["progress"]["result"]), (1, {"cost": 1.5}))
        self.assertEqual(rec["progress"]["seen"], [])
        mark = len(self.vm.calls)
        self.assertEqual(self.view()["cost_usd"], 1.5)
        self.assertEqual(self.vm.calls[mark:], [], "no sbx call at all for a finished, cached job")
        self.run_main("status", "--json")
        self.assertEqual(self.vm.calls[mark:], [("ls", "--json")])        # the one listing, nothing in the VM

    def test_cache_survives_the_sandbox_being_stopped(self):
        self.finished_job()
        self.view()
        self.vm.vm = "stopped"
        out = self.progress()
        self.assertIn("$1.50", out)
        self.assertNotIn("showing cached", out)                              # final values are not "stale"

    def test_stopped_sandbox_shows_cached_values_and_says_so(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE))
        self.assertEqual(self.view()["turns"], 1)
        self.vm.vm = "stopped"
        mark = len(self.vm.calls)
        out = self.progress()
        self.assertIn("sandbox stopped: showing cached values", out)
        v = self.view()
        self.assertEqual((v["turns"], v["stale"]), (1, True))
        self.assertEqual(self.execs()[len(self.execs()):], [])
        self.assertFalse([c for c in self.vm.calls[mark:] if c[0] == "exec"])    # LocalVM also refuses to boot it

    def test_missing_sandbox(self):
        self.run_job()
        self.vm.exists = False
        out = self.progress()
        self.assertIn("sandbox missing: showing cached values", out)

    def test_status_makes_one_extra_exec_per_running_job_only(self):
        job = self.run_job()
        self.set_output(job, asst("m1", usage=USAGE))
        mark = len(self.vm.calls)
        self.run_main("status", "--json")
        scripts = self.scripts_since(mark)
        self.assertEqual(len(scripts), 1)
        mark = len(self.vm.calls)
        self.run_main("status", "--json")
        self.assertEqual(len(self.scripts_since(mark)), 1)
        self.vm.vm = "stopped"
        mark = len(self.vm.calls)
        code, out, _ = self.run_main("status", "--json")
        self.assertEqual(code, 0)
        self.assertFalse([c for c in self.vm.calls[mark:] if c[0] == "exec"])

    def test_wait_caches_the_final_values(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "0"
        job = self.run_job()
        self.finish(job)
        self.assertEqual(ab.cmd_wait(self.prof, self.args()), 0)
        self.assertIn("progress", ab.job_record(self.prof))


class StatusShapeTests(ProgressCase):
    KEYS = {"elapsed_s", "last_event_age_s", "stalled", "turns", "tool_calls", "tokens", "cost_usd", "cost_is_estimate",
            "commits", "expect_commits", "plan", "note"}

    def status_job(self):
        code, out, _ = self.run_main("status", "--json")
        self.assertEqual(code, 0)
        return {r["name"]: r for r in json.loads(out)["profiles"]}

    def test_shape(self):
        job = self.run_job(expect_commits=2)
        (self.vm.jobdir(job) / "progress.tsv").write_text("done\ta\nactive\tb\npending\tc\nnote\thi\n")
        self.set_output(job, asst("m1", usage=USAGE, tools=["Bash"]))
        p = self.status_job()["p"]["job"]["progress"]
        self.assertTrue(self.KEYS <= set(p))
        self.assertEqual(set(p) - self.KEYS, {"tools_top", "stale"})
        self.assertEqual(set(p["tokens"]), {"input", "output", "cache_read", "cache_creation", "total"})
        self.assertEqual(p["plan"]["steps"], [{"text": "a", "status": "done"}, {"text": "b", "status": "active"},
                                              {"text": "c", "status": "pending"}])
        self.assertEqual((p["plan"]["current"], p["plan"]["done"], p["plan"]["total"], p["note"]), (2, 1, 3, "hi"))
        self.assertEqual((p["turns"], p["tool_calls"], p["expect_commits"], p["stalled"]), (1, 1, 2, False))
        self.assertIsInstance(p["elapsed_s"], int)
        self.assertEqual(p, self.view())                                      # progress --json is the same object

    def test_no_job_is_null(self):
        rows = self.status_job()
        self.assertIsNone(rows["p"]["job"]["progress"])
        self.assertIsNone(rows["sh"]["job"]["progress"])

    def test_human_suffix(self):
        job = self.run_job()
        (self.vm.jobdir(job) / "progress.tsv").write_text("done\ta\nactive\tb\n")
        self.set_output(job, asst("m1", usage=USAGE, ago=6))
        with mock.patch.object(ab, "sbx", lambda cfg, *a, **k: subprocess.CompletedProcess(a, 0, "", "")
                               if a == ("ls",) else self.vm(cfg, *a, **k)):
            code, out, _ = self.run_main("status")
        line = [ln for ln in out.splitlines() if ln.startswith("p ")][0]
        self.assertRegex(line, r"running 1/2 · \d+s · \d+s ago$")
        self.set_output(job, asst("m1", usage=USAGE, ago=500))
        (self.prof.state / ab.PROGRESS_CACHE).unlink()                     # recount from the start
        with mock.patch.object(ab, "sbx", lambda cfg, *a, **k: subprocess.CompletedProcess(a, 0, "", "")
                               if a == ("ls",) else self.vm(cfg, *a, **k)):
            code, out, _ = self.run_main("status")
        self.assertRegex([ln for ln in out.splitlines() if ln.startswith("p ")][0], r"running STALLED 8m$")


class WatchTests(ProgressCase):
    def test_non_tty_prints_plain_lines_and_exits_with_the_jobs_status(self):
        self.vm.env.update(FAKE_CLAUDE_SLEEP="1", FAKE_CLAUDE_EXIT="7")
        self.run_job()
        code, out, err = self.run_main("progress", "p", "--watch", "--interval", "1")
        self.assertEqual(code, 7, err)
        self.assertNotIn("\x1b", out)
        lines = out.strip().splitlines()
        self.assertTrue(lines and all(ln.startswith("job ") for ln in lines))
        self.assertIn("running", lines[0])
        self.assertIn("finished", lines[-1])

    def test_lost_job_exits_5(self):
        job = self.run_job()
        self.set_pid(job, "999999")
        code, out, _ = self.run_main("progress", "p", "--watch", "--interval", "1")
        self.assertEqual(code, ab.EXIT_LOST)

    def test_tty_redraws_in_place_with_our_own_escape_codes_only(self):
        self.vm.env["FAKE_CLAUDE_SLEEP"] = "1"
        job = self.run_job()
        (self.vm.jobdir(job) / "progress.tsv").write_text("note\t\x1b[31mred\x1b]0;title\x07\n")

        class Tty(__import__("io").StringIO):
            def isatty(self):
                return True
        buf = Tty()
        args = argparse.Namespace(job=None, watch=True, interval=1.0, json=False)
        with mock.patch.object(sys, "stdout", buf):
            self.assertEqual(ab.cmd_progress(self.prof, args), 0)
        text = buf.getvalue()
        self.assertTrue(text.startswith("\x1b[H\x1b[2J"))
        self.assertGreaterEqual(text.count("\x1b[H\x1b[2J"), 1)
        stripped = text.replace("\x1b[H\x1b[2J", "")
        self.assertNotIn("\x1b", stripped)                                    # nothing from the VM is raw
        self.assertNotIn("\x07", stripped)
        self.assertIn("\\x1b[31mred", stripped)

    def test_json_and_watch_conflict_and_bad_interval(self):
        self.run_job()
        self.assertEqual(self.run_main("progress", "p", "--json", "--watch")[0], 2)
        self.assertEqual(self.run_main("progress", "p", "--watch", "--interval", "0")[0], 2)

    def test_no_job_recorded(self):
        code, _, err = self.run_main("progress", "p")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
