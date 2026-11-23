import argparse
import base64
import io
import os
import re
import sys
import unittest
from unittest import mock

from helpers import AgentboxTestCase, ab

OSC = re.compile(r"\x1b\]7501;([^\x1b\x07]*)\x1b\\")
JOB = "20260101T000000Z-abcdef"


class Tty(io.StringIO):
    def isatty(self):
        return True


def records(text: str) -> list[dict]:
    """Every OSC 7501 report in `text`, as dicts with msg/title decoded."""
    out = []
    for body in OSC.findall(text):
        rec = dict(p.split("=", 1) for p in body.split(":"))
        for k in ("msg", "title"):
            if k in rec:
                rec[k] = base64.b64decode(rec[k]).decode()
        out.append(rec)
    return out


def view(stalled=False, age=5, plan=None, note=None, elapsed=600):
    return {"elapsed_s": elapsed, "last_event_age_s": age, "stalled": stalled, "turns": 1, "tool_calls": 0,
            "tools_top": [], "tokens": dict.fromkeys(("input", "output", "cache_read", "cache_creation", "total"), 0), "cost_usd": None, "cost_is_estimate": False, "commits": None,
            "expect_commits": None, "plan": plan, "note": note, "stale": False}


def job(state, code=None):
    return dict(ab.NO_JOB) | {"id": JOB if state != "none" else None, "state": state, "exit_code": code}


class EncoderTests(unittest.TestCase):
    def test_grammar(self):
        s = ab.osc7501({"state": "blocked", "kind": "other", "app": "agentbox", "id": "voicedev"})
        self.assertEqual(s, "\x1b]7501;state=blocked:kind=other:app=agentbox:id=voicedev\x1b\\")

    def test_msg_and_title_are_standard_base64_of_utf8(self):
        s = ab.osc7501({"state": "error", "msg": "exit 1 – ü", "title": "a?b>"})
        body = OSC.fullmatch(s).group(1)
        self.assertIn("msg=" + base64.b64encode("exit 1 – ü".encode()).decode(), body)
        self.assertIn("title=" + base64.b64encode(b"a?b>").decode(), body)
        self.assertEqual(records(s)[0]["msg"], "exit 1 – ü")

    def test_rejects_control_characters_in_text(self):
        for bad in ("a\nb", "a\rb", "\x1b]52;c;x\x07", "tab\there", "\x7f", "\x9b", "a\u2028b", "\u202eevil"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ab.osc7501({"state": "error", "msg": bad})

    def test_rejects_values_outside_the_charset(self):
        for bad in ("a:b", "a b", "a;b", "a\x1b", "", "a\\b", "é"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ab.osc7501({"state": bad})
        self.assertIn("progress=42", ab.osc7501({"state": "working", "progress": 42}))
        ab.osc7501({"state": "A-Za-z0-9_.,+/="})

    def test_rejects_bad_keys(self):
        for bad in ("Msg", "a-b", "", "1x", "a=b"):
            with self.assertRaises(ValueError, msg=bad):
                ab.osc7501({bad: "x"})

    def test_id_grammar(self):
        for good in ("voicedev", "a", "a/b", "A.b_c+d-e", "x" * 32, "/".join(["x" * 32] * 3)):
            ab.osc7501({"state": "idle", "id": good})
        for bad in ("", "x" * 33, "a//b", "/a", "a/", "a b", "a:b", "a,b", "a=b", "a/" + "x" * 33):
            with self.assertRaises(ValueError, msg=bad):
                ab.osc7501({"state": "idle", "id": bad})


class GateTests(unittest.TestCase):
    def gate(self, tty=True, env=None, as_json=False):
        env = {"REX_SESSION": "s1"} if env is None else env
        out = Tty() if tty else io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.dict(os.environ, env, clear=True):
            return ab.rex_wanted(as_json)

    def test_on_when_everything_allows_it(self):
        self.assertTrue(self.gate())

    def test_not_a_tty(self):
        self.assertFalse(self.gate(tty=False))

    def test_no_rex_session(self):
        self.assertFalse(self.gate(env={}))
        self.assertFalse(self.gate(env={"REX_SESSION": ""}))

    def test_json(self):
        self.assertFalse(self.gate(as_json=True))

    def test_agentbox_no_osc(self):
        self.assertFalse(self.gate(env={"REX_SESSION": "s1", "AGENTBOX_NO_OSC": "1"}))
        self.assertFalse(self.gate(env={"REX_SESSION": "s1", "AGENTBOX_NO_OSC": ""}))

    def test_off_reporter_writes_nothing(self):
        out = Tty()
        with mock.patch.object(sys, "stdout", out):
            r = ab.RexStatus(False)
            r.report({"state": "working"})
            r.report({"state": "working"}, "p")
            r.clear("p")
        self.assertEqual(out.getvalue(), "")


class MappingTests(unittest.TestCase):
    def test_table(self):
        plan = {"steps": [], "current": 2, "done": 1, "total": 3}
        cases = [
            (job("running"), view(), {"state": "working"}),
            (job("running"), None, {"state": "working"}),
            (job("running"), view(plan=plan), {"state": "working", "progress": 33}),
            (job("running"), view(plan=dict(plan, done=3)), {"state": "working", "progress": 100}),
            (job("running"), view(stalled=True, age=6 * 60 + 30),
             {"state": "blocked", "kind": "other", "msg": "no event for 6 min"}),
            (job("running"), view(stalled=True, age=None, elapsed=120),
             {"state": "blocked", "kind": "other", "msg": "no event for 2 min"}),
            (job("finished", 0), view(), {"state": "done"}),
            (job("finished", 1), view(), {"state": "error", "msg": "exit 1"}),
            (job("finished", 137), None, {"state": "error", "msg": "exit 137"}),
            (job("lost"), view(), {"state": "error", "msg": "job lost"}),
            (job("none"), None, {"state": "idle"}),
        ]
        for j, v, want in cases:
            self.assertEqual(ab.rex_fields(j, v), want, (j["state"], v))

    def test_every_mapped_report_encodes(self):
        for state, code in (("running", None), ("finished", 0), ("finished", 2), ("lost", None), ("none", None)):
            for v in (None, view(), view(stalled=True)):
                rec = records(ab.osc7501(ab.rex_fields(job(state, code), v)))
                self.assertEqual(len(rec), 1)

    def test_reporter_adds_app_and_id_and_dedupes(self):
        out = Tty()
        with mock.patch.object(sys, "stdout", out):
            r = ab.RexStatus(True)
            r.report({"state": "blocked", "kind": "other", "msg": "no event for 6 min"}, "voicedev")
            r.report({"state": "blocked", "kind": "other", "msg": "no event for 6 min"}, "voicedev")
            r.report({"state": "working"})
        self.assertTrue(out.getvalue().startswith(
            "\x1b]7501;state=blocked:kind=other:app=agentbox:id=voicedev:msg="))
        self.assertEqual(records(out.getvalue()),
                         [{"state": "blocked", "kind": "other", "app": "agentbox", "id": "voicedev",
                           "msg": "no event for 6 min"},
                          {"state": "working", "app": "agentbox"}])

    def test_profile_name_too_long_for_an_id_is_skipped(self):
        out = Tty()
        with mock.patch.object(sys, "stdout", out):
            r = ab.RexStatus(True)
            r.report({"state": "working"}, "a" * 41)
        self.assertEqual(out.getvalue(), "")


EVIL = "\x1b]52;c;ZXZpbA==\x07 rm -rf ~ \x1b[2J"


class NoVmTextTests(unittest.TestCase):
    """Plan steps, notes and tool names come from the VM: none of it may reach a report."""

    def test_vm_strings_never_in_reports(self):
        plan = {"steps": [{"text": EVIL, "status": "active"}, {"text": "step " + EVIL, "status": "done"}],
                "current": 1, "done": 1, "total": 2}
        v = view(plan=plan, note=EVIL) | {"tools_top": [{"name": EVIL, "count": 3}]}
        out = Tty()
        with mock.patch.object(sys, "stdout", out):
            r = ab.RexStatus(True)
            for j in (job("running"), job("finished", 0), job("finished", 3), job("lost")):
                r.report(ab.rex_fields(j | {"session_id": EVIL}, v))
                r.report(ab.rex_fields(j, v | {"stalled": True}), "p")
        text = out.getvalue()
        self.assertNotIn("\x1b]52", text)
        self.assertNotIn("rm -rf", text)
        self.assertNotIn(base64.b64encode(EVIL.encode()).decode(), text)
        for rec in records(text):
            self.assertNotIn("ZXZpbA", str(rec))
            self.assertNotIn("rm -rf", rec.get("msg", ""))
            self.assertRegex(rec.get("msg", "exit 0"), r"^(exit -?\d+|job lost|no event for \d+ min)$")
        self.assertEqual(OSC.sub("", text), "")       # nothing but our reports was written


class CommandCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        self.prof = self.cfg.profile("p")
        for name, val in (("resolve_job", lambda prof, a: JOB), ("cache_final_progress", lambda *a, **k: None)):
            p = mock.patch.object(ab, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(ab.time, "sleep", lambda s: None)
        p.start()
        self.addCleanup(p.stop)
        self.env(REX_SESSION="s1")

    def env(self, **kw):
        p = mock.patch.dict(os.environ, kw)
        p.start()
        self.addCleanup(p.stop)

    def run_cmd(self, fn, *a, tty=True):
        out = Tty() if tty else io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", io.StringIO()):
            try:
                code = fn(*a)
            except KeyboardInterrupt:
                code = "interrupted"
        return code, out.getvalue()


class ProgressWatchTests(CommandCase):
    def watch(self, states, views, tty=True, as_json=False, watch=True):
        js, vs = iter(states), iter(views)
        with mock.patch.object(ab, "job_info", lambda *a, **k: next(js)), \
                mock.patch.object(ab, "job_progress", lambda *a, **k: (next(vs), "")):
            args = argparse.Namespace(job=None, watch=watch, interval=1.0, json=as_json)
            return self.run_cmd(ab.cmd_progress, self.prof, args, tty=tty)

    def test_reports_the_profile_record_only_on_change(self):
        plan = {"steps": [{"text": EVIL, "status": "active"}], "current": 1, "done": 0, "total": 1}
        code, out = self.watch([job("running")] * 4 + [job("finished", 0)],
                               [view(plan=plan), view(plan=plan), view(stalled=True, age=420),
                                view(stalled=True, age=420), view()])
        self.assertEqual(code, 0)
        self.assertEqual(records(out), [{"state": "working", "app": "agentbox", "id": "p", "progress": "0"},
                                        {"state": "blocked", "kind": "other", "app": "agentbox", "id": "p",
                                         "msg": "no event for 7 min"},
                                        {"state": "done", "app": "agentbox", "id": "p"}])
        self.assertNotIn("\x1b]52", out)

    def test_failure_and_lost(self):
        _, out = self.watch([job("finished", 4)], [view()])
        self.assertEqual(records(out)[-1], {"state": "error", "app": "agentbox", "id": "p", "msg": "exit 4"})
        _, out = self.watch([job("lost")], [view()])
        self.assertEqual(records(out)[-1], {"state": "error", "app": "agentbox", "id": "p", "msg": "job lost"})

    def test_doubtful_lost_keeps_the_last_state(self):
        flaky = job("lost") | {"probe_failed": True}
        _, out = self.watch([job("running"), flaky, job("finished", 0)], [view()] * 3)
        self.assertEqual([r["state"] for r in records(out)], ["working", "done"])

    def test_ctrl_c_reports_idle(self):
        def boom(*a, **k):
            raise KeyboardInterrupt
        states = iter([job("running")])
        with mock.patch.object(ab, "job_info", lambda *a, **k: next(states)), \
                mock.patch.object(ab, "job_progress", lambda *a, **k: (view(), "")), \
                mock.patch.object(ab.time, "sleep", boom):
            args = argparse.Namespace(job=None, watch=True, interval=1.0, json=False)
            code, out = self.run_cmd(ab.cmd_progress, self.prof, args)
        self.assertEqual(code, "interrupted")
        self.assertEqual([r["state"] for r in records(out)], ["working", "idle"])

    def test_gates(self):
        self.assertEqual(records(self.watch([job("finished", 0)], [view()], tty=False)[1]), [])
        self.assertEqual(records(self.watch([job("finished", 0)], [view()], watch=False)[1]), [])
        self.assertEqual(records(self.watch([job("finished", 0)], [view()], as_json=True, watch=False)[1]), [])
        self.env(AGENTBOX_NO_OSC="1")
        self.assertEqual(records(self.watch([job("finished", 0)], [view()])[1]), [])

    def test_gate_no_rex_session(self):
        del os.environ["REX_SESSION"]
        self.assertEqual(records(self.watch([job("finished", 0)], [view()])[1]), [])


class WaitTests(CommandCase):
    def wait(self, states, timeout=None, tty=True):
        js = iter(states)
        with mock.patch.object(ab, "job_info", lambda *a, **k: next(js)):
            return self.run_cmd(ab.cmd_wait, self.prof, argparse.Namespace(job=None, timeout=timeout), tty=tty)

    def test_working_then_final(self):
        code, out = self.wait([job("running"), job("running"), job("finished", 3)])
        self.assertEqual(code, 3)
        self.assertEqual(records(out), [{"state": "working", "app": "agentbox", "id": "p"},
                                        {"state": "error", "app": "agentbox", "id": "p", "msg": "exit 3"}])
        _, out = self.wait([job("running"), job("finished", 0)])
        self.assertEqual([r["state"] for r in records(out)], ["working", "done"])
        _, out = self.wait([job("lost")])
        self.assertEqual(records(out), [{"state": "error", "app": "agentbox", "id": "p", "msg": "job lost"}])

    def test_not_a_tty(self):
        self.assertEqual(records(self.wait([job("running"), job("finished", 0)], tty=False)[1]), [])


class ProfileIdTests(CommandCase):
    """progress --watch and wait name the profile, so notify.lua can tell one job from another."""

    def test_progress_and_wait_report_the_profile_id(self):
        with mock.patch.object(ab, "job_info", lambda *a, **k: job("finished", 0)), \
                mock.patch.object(ab, "job_progress", lambda *a, **k: (view(), "")):
            _, a = self.run_cmd(ab.cmd_progress, self.prof,
                                argparse.Namespace(job=None, watch=True, interval=1.0, json=False))
            _, b = self.run_cmd(ab.cmd_wait, self.prof, argparse.Namespace(job=None, timeout=None))
        self.assertEqual(records(a), [{"state": "done", "app": "agentbox", "id": "p"}])
        self.assertEqual(records(b), [{"state": "done", "app": "agentbox", "id": "p"}])

    def test_profile_name_too_long_for_an_id_is_skipped(self):
        cfg = self.write_config(f'[profiles.{"a" * 40}]\nworkspace = "{self.ws}"\n')
        prof = cfg.profile("a" * 40)
        with mock.patch.object(ab, "job_info", lambda *a, **k: job("finished", 0)), \
                mock.patch.object(ab, "job_progress", lambda *a, **k: (view(), "")):
            _, a = self.run_cmd(ab.cmd_progress, prof,
                                argparse.Namespace(job=None, watch=True, interval=1.0, json=False))
            _, b = self.run_cmd(ab.cmd_wait, prof, argparse.Namespace(job=None, timeout=None))
        self.assertEqual((records(a), records(b)), ([], []))


class DashboardTests(CommandCase):
    def row(self, name, vm="running", j=None, v=None, enabled=True):
        return {"name": name, "enabled": enabled, "agent": "claude", "mode": "clone", "vm": vm, "resources": None,
                "job": None if j is None else j | {"progress": v}}

    def dash(self, frames, tty=True):
        it = iter(frames)

        def collect(cfg):
            try:
                rows = next(it)
            except StopIteration:
                raise KeyboardInterrupt
            return {"generated_at": 0, "sandboxes_known": rows is not None, "profiles": rows or []}
        with mock.patch.object(ab, "collect_dashboard", collect):
            return self.run_cmd(ab.cmd_dashboard, self.cfg,
                                argparse.Namespace(watch=True, json=False, interval=2.0), tty=tty)

    def test_one_record_per_profile(self):
        _, out = self.dash([
            [self.row("p", j=job("running"), v=view()), self.row("q", j=job("running"), v=view(stalled=True, age=60))],
            [self.row("p", j=job("finished", 0), v=view()), self.row("q", j=job("running"), v=view(stalled=True, age=60))],
        ])
        self.assertEqual(records(out), [
            {"state": "working", "app": "agentbox", "id": "p"},
            {"state": "blocked", "kind": "other", "app": "agentbox", "id": "q", "msg": "no event for 1 min"},
            {"state": "done", "app": "agentbox", "id": "p"},
            {"state": "clear", "id": "p"},              # Ctrl-C: nothing is watched any more
            {"state": "clear", "id": "q"},
        ])

    def test_finished_jobs_stay_after_the_vm_stops_until_the_next_job(self):
        lost = job("lost") | {"vm": "running"}          # process gone while the VM was up
        _, out = self.dash([
            [self.row("p", j=job("finished", 0)), self.row("q", j=job("finished", 2)), self.row("r", j=lost)],
            [self.row("p", "stopped", job("finished", 0)), self.row("q", "stopped", job("finished", 2)),
             self.row("r", "stopped", lost)],
            [self.row("p", "stopped", job("finished", 0)), self.row("q", "missing", job("finished", 2)),
             self.row("r", "stopped", lost)],
            [self.row("p", j=job("running"), v=view()), self.row("q", "missing", job("finished", 2)),
             self.row("r", "stopped", lost)],
        ])
        self.assertEqual(records(out)[:5], [
            {"state": "done", "app": "agentbox", "id": "p"},
            {"state": "error", "app": "agentbox", "id": "q", "msg": "exit 2"},
            {"state": "error", "app": "agentbox", "id": "r", "msg": "job lost"},
            {"state": "working", "app": "agentbox", "id": "p"},    # the next job replaces it
            {"state": "clear", "id": "p"},                         # Ctrl-C
        ])

    def test_working_whose_vm_stopped_becomes_error(self):
        died = job("lost") | {"vm": "stopped"}          # job_info's answer once the VM has stopped
        _, out = self.dash([
            [self.row("p", j=job("running"), v=view()), self.row("q", j=job("running"), v=view(stalled=True, age=60))],
            [self.row("p", "stopped", died), self.row("q", "missing", job("lost") | {"vm": "missing"})],
            [self.row("p", "stopped", died), self.row("q", "missing", job("lost") | {"vm": "missing"})],
        ])
        self.assertEqual([r for r in records(out) if r["state"] == "error"], [
            {"state": "error", "app": "agentbox", "id": "p", "msg": "sandbox stopped"},
            {"state": "error", "app": "agentbox", "id": "q", "msg": "sandbox missing"},
        ])

    def test_no_job_disabled_or_removed_clears(self):
        _, out = self.dash([
            [self.row("p", j=job("finished", 0)), self.row("q", j=job("finished", 0)),
             self.row("r", j=job("finished", 0))],
            [self.row("p", "stopped", job("none")), self.row("q", "disabled", enabled=False)],   # r: gone
        ])
        self.assertEqual([(r["state"], r["id"]) for r in records(out)],
                         [("done", "p"), ("done", "q"), ("done", "r"), ("clear", "p"), ("clear", "q"), ("clear", "r")])

    def test_running_without_a_job_is_idle_and_unknown_keeps_state(self):
        _, out = self.dash([
            [self.row("p", j=job("none"))],
            [self.row("p", vm="unknown")],
            None,                                       # `sbx ls` failed: no change
            [self.row("p", j=job("none"))],
            [self.row("p", "stopped", job("none"))],    # stopped, no job
        ])
        self.assertEqual(records(out), [{"state": "idle", "app": "agentbox", "id": "p"}, {"state": "clear", "id": "p"}])

    def test_unknown_keeps_a_finished_record(self):
        _, out = self.dash([[self.row("p", "stopped", job("finished", 0))], [self.row("p", "unknown")], None,
                            [self.row("p", "stopped", job("finished", 0))]])
        self.assertEqual([r["state"] for r in records(out)], ["done", "clear"])   # the clear is Ctrl-C

    def test_not_a_tty(self):
        _, out = self.dash([[self.row("p", j=job("running"), v=view())]], tty=False)
        self.assertEqual(records(out), [])


class DashboardStoppedVmTests(CommandCase):
    """collect_dashboard + dashboard_rex with the real job_info: a stopped VM is answered from the host."""

    def setUp(self):
        super().setUp()
        self.calls = []
        for name in ("vm_exec", "probe_job", "sbx_retry", "refresh_progress"):
            p = mock.patch.object(ab, name, lambda *a, _n=name, **k: self.calls.append(_n))
            p.start()
            self.addCleanup(p.stop)

    def collect(self, vm):
        with mock.patch.object(ab, "sandbox_states", lambda cfg: {self.prof.sandbox: vm}):
            data = ab.collect_dashboard(self.cfg)
        out = Tty()
        with mock.patch.object(sys, "stdout", out):
            ab.dashboard_rex(ab.RexStatus(True), data)
        return data, records(out.getvalue())

    def test_finished_job_on_a_stopped_vm(self):
        ab.save_job_record(self.prof, dict(ab.NO_JOB) | {"id": JOB, "state": "finished", "exit_code": 0, "vm": "running"})
        data, recs = self.collect("stopped")
        self.assertEqual(self.calls, [])
        self.assertEqual(data["profiles"][0]["job"]["state"], "finished")
        self.assertEqual(recs, [{"state": "done", "app": "agentbox", "id": "p"}])

    def test_running_job_on_a_stopped_vm_is_never_probed(self):
        ab.save_job_record(self.prof, dict(ab.NO_JOB) | {"id": JOB, "state": "running", "vm": "running"})
        for vm in ("stopped", "missing"):
            _, recs = self.collect(vm)
            self.assertEqual(self.calls, [])
            self.assertEqual(recs, [{"state": "error", "app": "agentbox", "id": "p", "msg": "sandbox stopped"}])

    def test_no_job_on_a_stopped_vm(self):
        _, recs = self.collect("stopped")
        self.assertEqual((self.calls, recs), ([], []))


if __name__ == "__main__":
    unittest.main()
