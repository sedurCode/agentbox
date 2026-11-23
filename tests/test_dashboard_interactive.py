"""`agentbox dashboard --interactive`: the pure parts (classification, renderer, key handler, sparkline,
history, collector) without a terminal, and the real curses loop under a pseudo-terminal."""
import itertools
import os
import select
import subprocess
import sys
import termios
import threading
import time
import unicodedata
import unittest
from unittest import mock

from helpers import REAL_POPEN, REAL_RUN, AgentboxTestCase, ab
from test_dashboard import SAMPLE, VIEW

NOW = 1_800_000_000.0
FINISHED = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 600))


def job(state="running", **kw):
    base = {"id": "20261123T164235Z-dbb67c", "state": state, "exit_code": None, "session_id": None,
            "started_at": "2026-11-23T16:42:35Z", "finished_at": None, "vm": "running", "progress": dict(VIEW)}
    if state == "finished":
        base |= {"exit_code": 0, "finished_at": FINISHED}
    return base | kw


def row(name, vm="running", j=None, **kw):
    return {"name": name, "enabled": True, "sandbox": f"ab-{name}", "agent": "claude", "mode": "clone", "vm": vm,
            "resources": ab.parse_vm_stats(SAMPLE) if vm == "running" else None, "job": j} | kw


def data(*rows, known=True):
    return {"generated_at": int(NOW), "sandboxes_known": known, "profiles": list(rows)}


ALL_STATES = data(
    row("zeta", j=job()),                                                         # working
    row("stall", j=job(progress=VIEW | {"stalled": True, "last_event_age_s": 900})),
    row("ok", vm="stopped", j=job("finished")),                                   # done, VM since stopped
    row("bad", j=job("finished", exit_code=3)),
    row("gone", vm="stopped", j=job("lost", finished_at=None, progress=None)),
    row("idle", j=dict(ab.NO_JOB)),
    row("cold", vm="stopped", j=dict(ab.NO_JOB)),
    row("never", vm="missing", j=dict(ab.NO_JOB)),
    {"name": "off", "enabled": False, "vm": "disabled"},
    row("flaky", vm="unknown"),
    {"name": "broken", "enabled": True, "vm": "unknown", "error": True},
    row("alpha", j=job()),
)


def text_of(lines):
    return "\n".join("".join(t for t, _ in ln) for ln in lines)


def ui_for(d, size=(120, 30), **kw):
    ui = ab.dash_new_ui(size)
    return ab.dash_set_rows(ui, [r["name"] for r in ab.dash_sort((d or {}).get("profiles") or [])]) | kw


class ClassifyTests(unittest.TestCase):
    def test_every_display_state(self):
        got = {r["name"]: ab.dash_state(r) for r in ALL_STATES["profiles"]}
        self.assertEqual(got, {"zeta": "working", "stall": "stalled", "ok": "done", "bad": "error", "gone": "lost",
                               "idle": "nojob", "cold": "stopped", "never": "stopped", "off": "disabled",
                               "flaky": "unknown", "broken": "unknown", "alpha": "working"})

    def test_sort_working_then_attention_then_finished_then_rest_by_name(self):
        names = [r["name"] for r in ab.dash_sort(ALL_STATES["profiles"])]
        self.assertEqual(names, ["alpha", "zeta", "bad", "gone", "stall", "ok",
                                 "broken", "cold", "flaky", "idle", "never", "off"])

    def test_every_state_has_a_glyph_in_both_sets(self):
        for g, ga, *_ in ab.DASH_STATES.values():
            self.assertEqual(ab.dash_cells(g), 1)
            self.assertTrue(ga.isascii() and len(ga) == 1)

    def test_time_column(self):
        rows = {r["name"]: r for r in ALL_STATES["profiles"]}
        self.assertEqual(ab.dash_when(rows["zeta"], NOW), "12m")              # elapsed of a running job
        self.assertEqual(ab.dash_when(rows["ok"], NOW), "10m ago")            # since it finished
        self.assertEqual(ab.dash_when(rows["gone"], NOW), "-")                # no finish time known
        self.assertEqual(ab.dash_when(rows["cold"], NOW), "")


class RenderTests(unittest.TestCase):
    SIZES = [(0, 0), (1, 1), (20, 5), (59, 40), (200, 14), (60, 15), (70, 20), (99, 40), (100, 15), (120, 30), (240, 70)]

    def check(self, lines, size):
        w, h = size
        self.assertEqual(len(lines), h if w > 0 else 0, size)
        for i, ln in enumerate(lines):
            self.assertEqual(sum(ab.dash_cells(t) for t, _ in ln), w, f"{size} line {i}: {ln!r}")
            for t, st in ln:
                for ch in t:
                    self.assertNotIn(unicodedata.category(ch), ("Cc", "Cf", "Cs", "Co", "Cn", "Mn", "Me", "Zl", "Zp"),
                                     f"{size} line {i}: {t!r}")
                self.assertIsInstance(st, str)

    def render(self, d, size=(120, 30), ascii_=False, history=None, **ui):
        hist = history or ab.DashHistory()
        lines = ab.dash_render(d, ui_for(d, size) | ui, hist, size, now=NOW, ascii_=ascii_)
        self.check(lines, size)
        return lines

    def test_every_line_is_exactly_the_width_at_every_size(self):
        for size in self.SIZES:
            for d in (None, data(), ALL_STATES):
                for mode in ("list", "help", "confirm"):
                    for ascii_ in (False, True):
                        with self.subTest(size=size, mode=mode, ascii_=ascii_):
                            self.render(d, size, ascii_, mode=mode, target="alpha", busy=True)

    def test_too_small_asks_for_a_bigger_pane(self):
        for size in ((59, 30), (120, 14), (30, 5)):
            text = text_of(self.render(ALL_STATES, size))
            self.assertIn("make the pane bigger", text)
            self.assertNotIn("alpha", text)

    def test_wide_puts_the_details_beside_the_list(self):
        lines = self.render(ALL_STATES, (120, 30))
        self.assertIn("agentbox", text_of(lines[:1]))
        first = text_of([lines[1]])
        self.assertIn("alpha", first)                     # the list's first row...
        self.assertIn("│", first)                          # ...and the details on the same line
        self.assertIn("working", first)
        self.assertIn("q quit", text_of(lines[-1:]))

    def test_narrow_stacks_the_details_under_the_list(self):
        lines = self.render(ALL_STATES, (70, 30))
        text = text_of(lines)
        self.assertNotIn("│", text)
        rule = next(i for i, ln in enumerate(lines) if text_of([ln]).startswith("──"))
        self.assertIn("of 12", text_of([lines[rule]]))     # the list is scrolled: says which part shows
        self.assertTrue(any("alpha  working" in text_of([ln]) for ln in lines[rule:]))

    def test_header_counts_running_and_flags_a_failed_listing(self):
        head = text_of(self.render(ALL_STATES, (120, 30), busy=True)[:1])
        self.assertIn(f"{sum(r['vm'] == 'running' for r in ALL_STATES['profiles'])} running", head)
        self.assertIn("refreshing", head)
        self.assertNotIn("could not list", head)
        head = text_of(self.render(data(row("a"), known=False), (120, 30))[:1])
        self.assertIn("could not list sandboxes", head)

    def test_details_show_vm_job_and_plan(self):
        view = VIEW | {"tools_top": [{"name": "Bash", "count": 30}, {"name": "Read", "count": 9}],
                       "plan": {"steps": [{"text": "read", "status": "done"}, {"text": "write", "status": "active"},
                                          {"text": "test", "status": "pending"}], "current": 2, "done": 1, "total": 3},
                       "note": "halfway", "expect_commits": 3}
        text = text_of(self.render(data(row("a", j=job(progress=view))), (120, 40)))
        for want in ("up 1h02m", "cpu", "50%", "memory", "25%", "disk", "job  20261123T164235Z-dbb67c  running",
                     "elapsed 12m04s", "idle 6s", "turns 88", "tool calls 61", "Bash 30, Read 9", "commits 1/3",
                     "tokens 4.1M", "~$1.84", "plan 1/3", "agent's plan, not verified", "✓ read", "▶ write",
                     "· test", "note halfway", "agent's note"):
            self.assertIn(want, text)

    def test_exact_cost_has_no_tilde(self):
        d = data(row("a", j=job("finished", progress=VIEW | {"cost_is_estimate": False, "cost_usd": 2.5})))
        text = text_of(self.render(d))
        self.assertIn("cost $2.50", text)
        self.assertIn("exit 0", text)

    def test_null_fields_show_a_dash(self):
        nulls = {k: None for k in VIEW}
        res = {"uptime_s": None, "cpus": None, "load": None, "cpu_pct": None, "mem_total": 0, "mem_used": None,
               "disk_total": None, "disk_used": None}
        for d in (data(row("a", j=job(progress=nulls), resources=res)),
                  data(row("a", j=job(progress=VIEW | {"tokens": {"total": None}, "plan": {"steps": [{}]}}))),
                  data(row("a", j={"id": "x", "state": "running"}, resources={})),
                  data(row("a", j=job("finished", exit_code=None, progress=None), resources=None)),
                  data({"name": "bare"})):
            with self.subTest(d=d):
                self.render(d, (120, 30))
                self.render(d, (60, 15), ascii_=True)
        text = text_of(self.render(data(row("a", j=job(progress=nulls))), (120, 30)))
        for want in ("elapsed -", "turns -", "tool calls -", "tokens -", "cost -", "idle -", "commits -"):
            self.assertIn(want, text)

    def test_hostile_text_is_sanitised(self):
        evil = "x\x1b]52;c;cHduZWQ=\x07y\x1b[2J\r‮​́\x9b\ttab\nnl"
        view = VIEW | {"plan": {"steps": [{"text": evil, "status": "active"}], "current": 1, "done": 0, "total": 1},
                       "note": evil, "tools_top": [{"name": evil, "count": 1}]}
        d = data(row(evil, j=job(id="j\x1b[2J" + evil, progress=view), agent=evil, mode=evil, vm="running"))
        for size in ((120, 30), (70, 20)):
            text = text_of(self.render(d, size))
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\x07", text)
            self.assertIn("\\x1b", text)                  # visible instead

    def test_wide_characters_keep_the_width(self):
        view = VIEW | {"plan": {"steps": [{"text": "日本語のテキスト" * 20, "status": "active"}],
                                "current": 1, "done": 0, "total": 1}}
        for w in range(60, 130):
            self.render(data(row("a", j=job(progress=view))), (w, 20))

    def test_ascii_fallback_draws_only_ascii(self):
        view = VIEW | {"plan": {"steps": [{"text": "a", "status": "done"}, {"text": "b", "status": "active"}],
                                "current": 2, "done": 1, "total": 2}}
        hist = ab.DashHistory()
        for d in (ALL_STATES, data(row("a", j=job(progress=view)))):
            hist.add(d)
            hist.add(d)
            for size in ((120, 40), (70, 40)):
                for mode in ("list", "help", "confirm"):
                    text = text_of(self.render(d, size, ascii_=True, history=hist, mode=mode, target="a", busy=True))
                    self.assertTrue(text.isascii(), [c for c in text if not c.isascii()])

    def test_selection_is_highlighted_and_scrolled_into_view(self):
        d = data(*[row(f"p{i:02d}") for i in range(40)])
        ui = ui_for(d, (70, 20))
        ui, _ = ab.dash_key(ui, "end")
        lines = ab.dash_render(d, ui, ab.DashHistory(), (70, 20), now=NOW)
        sel = [text_of([ln]) for ln in lines if any("sel" in st.split() for _, st in ln)]
        self.assertEqual(len(sel), 1)
        self.assertIn("p39", sel[0])

    def test_confirm_shows_the_exact_command(self):
        text = text_of(self.render(ALL_STATES, (70, 20), mode="confirm", target="alpha"))
        self.assertIn("agentbox fetch alpha", text)
        self.assertNotIn("--", text.replace("──", ""))

    def test_sparklines_appear_after_two_samples(self):
        hist = ab.DashHistory()
        d1 = data(row("a", j=job()))
        hist.add(d1)
        self.assertIn("after the next refresh", text_of(self.render(d1, history=hist)))
        hist.add(data(row("a", j=job(progress=VIEW | {"tokens": VIEW["tokens"] | {"total": 5_000_000}}))))
        text = text_of(self.render(d1, history=hist))
        self.assertIn("█", text)
        self.assertIn("per refresh", text)


class SparklineHistoryTests(unittest.TestCase):
    def test_sparkline(self):
        self.assertEqual(ab.sparkline([0, 1, 2, 4, 8], 5), "▁▂▃▅█")
        self.assertEqual(ab.sparkline([0, 0], 4), "  ▁▁")                 # right-aligned, zero is the floor
        self.assertEqual(ab.sparkline([5, None, 5], 3), "█ █")             # None is a gap
        self.assertEqual(ab.sparkline(range(100), 10), ab.sparkline(range(90, 100), 10))   # the newest only
        self.assertEqual(ab.sparkline([], 3), "   ")
        self.assertEqual(ab.sparkline([1, 2], 0), "")
        self.assertEqual(ab.sparkline([0, 1, 8], 3, ascii_=True), "_.#")
        self.assertEqual(ab.sparkline([1, 1000000], 2)[0], "▂")            # anything above 0 shows

    def test_history_is_a_ring_per_job(self):
        h = ab.DashHistory(size=3)
        for n in range(6):
            h.add(data(row("a", j=job(progress=VIEW | {"tokens": {"total": n * 10}, "tool_calls": n}))))
        self.assertEqual(h.totals("20261123T164235Z-dbb67c", "tokens"), [20, 30, 40, 50])
        self.assertEqual(h.deltas("20261123T164235Z-dbb67c", "tools"), [1, 1, 1])
        self.assertEqual(h.deltas("nope", "tokens"), [])

    def test_history_survives_nulls_and_resets(self):
        h, jid = ab.DashHistory(), "20261123T164235Z-dbb67c"
        for view in ({"tokens": {"total": 10}}, {"tokens": None}, {"tokens": {"total": 30}}, {"tokens": {"total": 5}}):
            h.add(data(row("a", j=job(progress=view))))
        self.assertEqual(h.deltas(jid, "tokens"), [None, None, 0])       # a recount never goes negative
        h.add(data(row("a", j=job(progress=None)), row("b", j=None), {"name": "c"}))
        h.add(None)
        self.assertEqual(len(h.totals(jid, "tokens")), 4)

    def test_history_keeps_only_recent_jobs(self):
        h = ab.DashHistory(jobs=2)
        for jid in ("j1", "j2", "j3"):
            h.add(data(row("a", j=job(id=jid))))
        self.assertEqual(list(h.jobs), ["j2", "j3"])


class KeyTests(unittest.TestCase):
    def setUp(self):
        self.ui = ui_for(ALL_STATES, (120, 30))          # names: alpha, zeta, bad, ...

    def keys(self, *keys, ui=None):
        ui, actions = ui or self.ui, []
        for k in keys:
            ui, act = ab.dash_key(ui, k)
            actions.append(act)
        return ui, actions

    def test_moves_and_clamps(self):
        n = len(self.ui["names"])
        self.assertEqual(self.keys("j", "down", "j")[0]["sel"], 3)
        self.assertEqual(self.keys("j", "k", "up", "up")[0]["sel"], 0)
        self.assertEqual(self.keys("end")[0]["sel"], n - 1)
        self.assertEqual(self.keys("end", "j", "home")[0]["sel"], 0)
        self.assertEqual(self.keys("G", "pgdn")[0]["sel"], n - 1)

    def test_actions_name_the_selected_profile(self):
        _, acts = self.keys("j", "enter", "p", "l")
        self.assertEqual(acts[1:], [("progress", "zeta"), ("progress", "zeta"), ("logs", "zeta")])
        self.assertEqual(self.keys("r")[1], [("refresh",)])
        self.assertEqual(self.keys("q")[1], [("quit",)])
        self.assertEqual(self.keys("ctrl-c")[1], [("quit",)])

    def test_fetch_only_after_y(self):
        ui, acts = self.keys("f")
        self.assertEqual(acts, [("confirm_fetch", "alpha")])
        self.assertEqual(ui["mode"], "confirm")
        self.assertEqual(self.keys("y", ui=ui)[1], [("fetch", "alpha")])
        self.assertEqual(self.keys("Y", ui=ui)[1], [("fetch", "alpha")])
        for no in ("n", "N", "enter", "esc", "f", "q", "j", "ctrl-c", " ", ("click", 1, 2), "resize"):
            ui2, acts = self.keys(no, "y", ui=ui)
            if no == "resize":                       # a resize redraws and leaves the question open
                self.assertEqual(acts, [("redraw",), ("fetch", "alpha")])
                continue
            self.assertEqual(acts, [None, None], no)
            self.assertEqual(ui2["mode"], "list")
        self.assertEqual(self.keys("n", ui=ui)[0]["msg"], "fetch cancelled")
        self.assertEqual(self.keys("y")[1], [None])  # no question asked: y does nothing

    def test_no_key_sequence_runs_anything_but_the_three_commands(self):
        keys = ["f", "y", "Y", "j", "enter", "p", "l", "?", "q", "r", "n", "end", "resize", ("click", 3, 2),
                ("wheel", 1, 80, 5), "-", "F", "\x1b"]
        allowed = {"progress": ["progress", None, "--watch"], "logs": ["logs", None], "fetch": ["fetch", None]}
        for seq in itertools.product(keys, repeat=3):
            ui = self.ui
            for k in seq:
                before = ui["mode"]
                ui, act = ab.dash_key(ui, k)
                argv = ab.dash_argv(act) if act else None
                if argv is not None:
                    self.assertEqual(argv, [a if a is not None else act[1] for a in allowed[argv[0]]])
                    self.assertFalse(any(a.startswith("-") and a != "--watch" for a in argv), argv)
                    self.assertNotIn("--force", argv)
                if act and act[0] == "fetch":            # only from the question that `f` opens
                    self.assertEqual((before, k in ("y", "Y")), ("confirm", True))
                if ui["mode"] == "confirm" and before != "confirm":
                    self.assertEqual((k, act and act[0]), ("f", "confirm_fetch"))

    def test_dash_argv(self):
        self.assertEqual(ab.dash_argv(("fetch", "a-b")), ["fetch", "a-b"])
        self.assertEqual(ab.dash_argv(("progress", "a")), ["progress", "a", "--watch"])
        self.assertEqual(ab.dash_argv(("logs", "a")), ["logs", "a"])
        for bad in (("fetch", "--force"), ("fetch", "-x"), ("fetch", None), ("fetch", "a b"), ("down", "a"),
                    ("run", "a"), ("confirm_fetch", "a"), ("quit",), ()):
            self.assertIsNone(ab.dash_argv(bad), bad)

    def test_help_overlay(self):
        ui, acts = self.keys("?")
        self.assertEqual((ui["mode"], acts), ("help", [None]))
        ui2, acts = self.keys("j", ui=ui)
        self.assertEqual((ui2["mode"], ui2["sel"], acts), ("list", 0, [None]))    # closes, does nothing else
        self.assertEqual(self.keys("q", ui=ui)[1], [("quit",)])

    def test_mouse(self):
        lay = ab.dash_layout(120, 30, len(self.ui["names"]))
        lx, ly, lw, lh = lay["list"]
        self.assertEqual(self.keys(("click", 2, ly + 2))[0]["sel"], 2)
        self.assertEqual(self.keys(("click", 2, ly + 50))[0]["sel"], 0)            # below the rows
        self.assertEqual(self.keys(("click", lw + 5, ly + 2))[0]["sel"], 0)        # in the details
        many = ui_for(data(*[row(f"p{i:02d}") for i in range(60)]), (120, 30))
        ui, _ = self.keys(("wheel", 1, 2, ly), ("wheel", 1, 2, ly), ui=many)
        self.assertEqual((ui["top"], ui["sel"]), (6, 0))
        self.assertEqual(self.keys(("click", 2, ly), ui=ui)[0]["sel"], 6)
        ui, _ = self.keys(*[("wheel", 1, 2, ly)] * 50, ui=many)
        self.assertEqual(ui["top"], 60 - lh)
        ui, _ = self.keys(("wheel", 1, lw + 5, ly), ("wheel", -1, lw + 5, ly), ("wheel", 1, lw + 5, ly))
        self.assertEqual(ui["dtop"], 3)

    def test_new_data_keeps_the_selected_profile(self):
        ui, _ = self.keys("j", "j")
        self.assertEqual(ui["names"][ui["sel"]], "bad")
        ui = ab.dash_set_rows(ui, ["bad", "alpha"])
        self.assertEqual(ui["sel"], 0)
        ui = ab.dash_set_rows(ui, ["x"])
        self.assertEqual(ui["sel"], 0)
        ui = ab.dash_set_rows(ui, [])
        self.assertEqual(self.keys("enter", "f", "l", "j", ui=ui)[1], [None, None, None, None])

    def test_resize_keeps_the_selection_visible(self):
        many = ui_for(data(*[row(f"p{i:02d}") for i in range(60)]), (120, 60))
        ui, _ = self.keys("end", ui=many)
        ui, act = ab.dash_key(ui | {"size": (70, 20)}, "resize")
        lh = ab.dash_layout(70, 20, 60)["list"][3]
        self.assertEqual(act, ("redraw",))
        self.assertTrue(ui["top"] <= ui["sel"] < ui["top"] + lh)


class CollectorTests(unittest.TestCase):
    def test_never_two_at_once(self):
        gate, calls = threading.Event(), []

        def collect():
            calls.append(1)
            gate.wait(5)
            return {"n": len(calls)}
        c = ab.DashCollector(collect)
        self.assertTrue(c.start())
        self.assertFalse(c.start())
        self.assertTrue(c.running)
        self.assertIsNone(c.take())
        gate.set()
        for _ in range(500):
            if not c.running:
                break
            time.sleep(0.01)
        self.assertEqual(c.take(), ("ok", {"n": 1}))
        self.assertIsNone(c.take())
        self.assertEqual(len(calls), 1)

    def test_errors_do_not_escape(self):
        for exc in (RuntimeError("boom"), SystemExit(1)):
            def collect(exc=exc):
                raise exc
            c = ab.DashCollector(collect)
            c.start()
            for _ in range(500):
                if not c.running:
                    break
                time.sleep(0.01)
            kind, e = c.take()
            self.assertEqual((kind, e), ("error", exc))


class FakeCurses:
    """Just enough of the curses module for DashScreen.setup()."""
    error = Exception
    A_BOLD, A_DIM, A_REVERSE = 1, 2, 4
    COLOR_BLACK, COLOR_RED, COLOR_GREEN, COLOR_YELLOW, COLOR_CYAN = range(5)
    BUTTON1_PRESSED = BUTTON1_CLICKED = BUTTON4_PRESSED = BUTTON5_PRESSED = 0

    def __init__(self):
        self.pairs, self.default_colors = [], False

    def has_colors(self):
        return True

    def use_default_colors(self):
        self.default_colors = True

    def init_pair(self, i, fg, bg):
        self.pairs.append((i, fg, bg))

    def color_pair(self, i):
        return 256 * i

    def __getattr__(self, name):         # curs_set, mousemask, mouseinterval
        return lambda *a: None


class ColourTests(unittest.TestCase):
    def screen(self, env):
        c, scr = FakeCurses(), mock.Mock()
        with mock.patch.dict(os.environ, env):
            if "NO_COLOR" not in env:
                os.environ.pop("NO_COLOR", None)
            s = ab.DashScreen(scr, c, lambda: None, 5, ab.RexStatus(False), False)
            s.setup()
        return s, c

    def test_terminal_palette_and_default_background(self):
        s, c = self.screen({})
        self.assertTrue(c.default_colors)
        self.assertTrue(c.pairs and all(bg == -1 for _, _, bg in c.pairs))
        self.assertEqual(s.attr("red sel"), 256 | 4)

    def test_no_color_means_attributes_only(self):
        s, c = self.screen({"NO_COLOR": "1"})
        self.assertEqual(c.pairs, [])
        self.assertEqual((s.attr("red"), s.attr("green"), s.attr("sel dim")), (1, 0, 6))


class CommandTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.a]\nworkspace = "{self.ws}"\n')

    def test_needs_a_terminal_and_no_json_or_watch(self):
        for argv in (("-i",), ("--interactive",), ("-i", "--json"), ("-i", "--watch"), ("-i", "--interval", "1")):
            code, out, err = self.run_main("dashboard", *argv)
            self.assertEqual(code, 2, argv)
            self.assertEqual(out, "")
        self.assertIn("needs a terminal", self.run_main("dashboard", "-i")[2])
        self.assertIn("cannot be combined", self.run_main("dashboard", "-i", "--json")[2])

    def test_pager_is_split_without_a_shell(self):
        with mock.patch.dict(os.environ, {"PAGER": "less -R -S"}):
            self.assertEqual(ab.dash_pager(), ["less", "-R", "-S"])
        for v in ("", "'unclosed"):
            with mock.patch.dict(os.environ, {"PAGER": v}):
                self.assertEqual(ab.dash_pager(), ["less", "-R"])


@unittest.skipUnless(hasattr(os, "fork") and sys.platform != "win32", "needs a pseudo-terminal")
class PtyTests(AgentboxTestCase):
    """The real curses loop under a pseudo-terminal, with collect_dashboard stubbed."""

    def read_until(self, fd, want: bytes, buf: bytearray, timeout=15.0, start=0) -> bool:
        """Read the pty into buf until `want` shows up after offset `start`."""
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

    def spawn(self, env=None, child_cmd=False):
        """Fork main(["dashboard", "--interactive"]) onto a 110x30 pseudo-terminal, with collect_dashboard
        stubbed. child_cmd: the commands the dashboard runs become a Python one-liner printing its argv."""
        import fcntl
        import pty
        import struct
        self.write_config(f'[profiles.demo]\nworkspace = "{self.ws}"\n[profiles.other]\nworkspace = "{self.ws}"\n')
        fake = data(row("demo", j=job()), row("other", vm="stopped", j=job("finished", exit_code=2)))
        pid, fd = pty.fork()
        if pid == 0:                                       # the child: run main() on the pty
            code = 98
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
                os.environ.update(TERM="xterm-256color", REX_SESSION="test", **(env or {}))
                os.environ.pop("AGENTBOX_NO_OSC", None)
                os.environ.pop("NO_COLOR", None)

                def collect(cfg):
                    time.sleep(0.2)                          # slow enough to be seen refreshing
                    return fake | {"generated_at": int(time.time())}
                stub = [sys.executable, "-c", "import os, sys; print('CHILD', sys.argv[1:], os.environ.get('AGENTBOX_NO_OSC'))"]
                with mock.patch.object(ab, "collect_dashboard", collect), \
                        mock.patch.object(ab, "agentbox_argv", lambda args: stub + args if child_cmd else ["false"]), \
                        mock.patch.object(subprocess, "run", REAL_RUN), mock.patch.object(subprocess, "Popen", REAL_POPEN), \
                        mock.patch.object(sys, "argv", ["agentbox", "dashboard", "--interactive"]):
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
        return pid, fd

    def finish(self, pid, fd, buf) -> bytes:
        """Wait for the child to exit 0 after `q`; check the terminal was given back. Returns all output."""
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
            self.fail("the dashboard did not exit after q")
        self.read_until(fd, b"\x00never", buf, timeout=0.3)
        out = bytes(buf)
        self.assertTrue(os.WIFEXITED(status), status)
        self.assertEqual(os.WEXITSTATUS(status), 0, out[-3000:])
        self.assertNotIn(b"Traceback", out)
        self.assertIn(b"\x1b[?1049h", out)              # the alternate screen, entered...
        self.assertIn(b"\x1b[?1049l", out[out.rindex(b"\x1b[?1049h"):])   # ...and left after the last entry
        attrs = termios.tcgetattr(fd)
        self.assertTrue(attrs[3] & termios.ICANON and attrs[3] & termios.ECHO, "terminal left in raw mode")
        return out

    def press(self, fd, buf, *steps):
        for key, want in steps:
            start = len(buf)
            os.write(fd, key)
            if want:
                self.assertTrue(self.read_until(fd, want, buf, start=start), (key, bytes(buf[start:][-2000:])))
            else:
                time.sleep(0.2)

    def test_keys_then_q_restores_the_terminal(self):
        pid, fd = self.spawn()
        buf = bytearray()
        self.assertTrue(self.read_until(fd, b"other", buf), buf[-2000:])
        self.press(fd, buf, (b"j", None), (b"k", None), (b"?", b"any key closes"), (b"x", None),
                   (b"f", b"agentbox fetch demo"), (b"n", b"fetch cancelled"), (b"r", None), (b"q", None))
        out = self.finish(pid, fd, buf)
        self.assertIn(b"\x1b]7501;state=working:app=agentbox:id=demo", out)    # Rex records, straight out
        self.assertIn(b"\x1b]7501;state=clear:id=demo\x1b\\", out)             # cleared on exit

    def test_ctrl_c_quits_cleanly(self):
        pid, fd = self.spawn()
        buf = bytearray()
        self.assertTrue(self.read_until(fd, b"other", buf), buf[-2000:])
        self.press(fd, buf, (b"?", b"any key closes"), (b"\x03", None))
        out = self.finish(pid, fd, buf)
        self.assertIn(b"\x1b]7501;state=clear:id=demo\x1b\\", out)

    def test_commands_run_in_the_terminal_and_come_back(self):
        pid, fd = self.spawn(env={"PAGER": "cat"}, child_cmd=True)
        buf = bytearray()
        self.assertTrue(self.read_until(fd, b"other", buf), buf[-2000:])
        back = b"\x1b[?1049h"                               # curses takes the screen again
        self.press(fd, buf, (b"\r", b"press Enter to return"), (b"\r", back),
                   (b"l", b"CHILD ['logs', 'demo'] 1"), (b"j", None),
                   (b"f", b"agentbox fetch other"), (b"y", b"press Enter to return"), (b"\r", back),
                   (b"q", None))
        out = self.finish(pid, fd, buf)
        self.assertIn(b"CHILD ['progress', 'demo', '--watch'] 1", out)
        self.assertIn(b"CHILD ['fetch', 'other'] 1", out)
        self.assertNotIn(b"--force", out)

if __name__ == "__main__":
    unittest.main()
