"""Anything that came out of the VM is agent-controlled: it must not reach the host terminal as live escapes."""
import contextlib
import io
import json
import re
import unittest
from unittest import mock

import test_jobs
from helpers import AgentboxTestCase, FakeSbx, ab, needs_hostile_filenames

ESC, BEL, CSI8 = "\x1b", "\x07", "\x9b"
OSC52 = f"{ESC}]52;c;ZWNobyBwd25lZAo={BEL}"            # "set the host clipboard"
CSI = f"{ESC}[2J{ESC}[H"                               # clear screen
# Any of these reaching the terminal is a bug: C0 (bar \t \n), DEL, C1, bidi overrides.
LIVE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


class SafeTextTests(unittest.TestCase):
    def test_escapes(self):
        self.assertEqual(ab.safe_text(f"a{ESC}b"), "a\\x1bb")
        self.assertEqual(ab.safe_text(OSC52), "\\x1b]52;c;ZWNobyBwd25lZAo=\\x07")
        self.assertEqual(ab.safe_text(CSI), "\\x1b[2J\\x1b[H")
        self.assertEqual(ab.safe_text(f"x{CSI8}31m"), "x\\x9b31m")                 # 8-bit CSI
        self.assertEqual(ab.safe_text("\x00\x08\x0d\x7f\x85\x9f"), "\\x00\\x08\\x0d\\x7f\\x85\\x9f")
        self.assertEqual(ab.safe_text("a‮b "), "a\\u202eb\\u2028")

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(ab.safe_text("tab\there\nnewline é 日本 ✓"), "tab\there\nnewline é 日本 ✓")
        self.assertEqual(ab.safe_text(123), "123")

    def test_nothing_unsafe_survives(self):
        everything = "".join(map(chr, range(0x300)))
        self.assertIsNone(LIVE.search(ab.safe_text(everything)))

    def test_die_and_info_escape_their_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ab.info(f"stderr from the VM: {OSC52}")
            with self.assertRaises(SystemExit):
                ab.die(f"sbx said {CSI}")
        self.assertIsNone(LIVE.search(err.getvalue().replace("\n", "")))
        self.assertIn("\\x1b]52;c;", err.getvalue())

    def test_job_summary_validates_probe_values(self):
        s = ab.job_summary("j", "finished", {"exit_code": "0", "session_id": f"sess{ESC}[31m1234",
                                             "started_at": f"2026-01-01T00:00:00Z{ESC}", "finished_at": "2026-01-01T00:00:01Z"})
        self.assertIsNone(s["session_id"])
        self.assertIsNone(s["started_at"])
        self.assertEqual(s["finished_at"], "2026-01-01T00:00:01Z")


class RenderTests(unittest.TestCase):
    def render(self, ev):
        return ab.render_stream_line(json.dumps(ev) if not isinstance(ev, str) else ev)

    def test_every_rendered_field_is_safe(self):
        events = [
            {"type": "assistant", "message": {"content": [{"type": "text", "text": f"hi {OSC52} {CSI} {CSI8}"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": f"Bash{ESC}[31m",
                                                           "input": {"command": f"echo {OSC52}"}}]}},
            {"type": "system", "subtype": "init", "session_id": f"s{ESC}]0;title{BEL}", "model": f"m{CSI8}"},
            {"type": "result", "subtype": f"ok{ESC}", "result": f"done{BEL}"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": {"nested": ESC}}, "junk"]}},
        ]
        for ev in events:
            out = self.render(ev)
            self.assertIsNotNone(out, ev)
            self.assertIsNone(LIVE.search(out.replace("\n", "")), repr(out))

    def test_unparseable_line_is_escaped(self):
        out = self.render(f"not json {OSC52} {CSI8}")
        self.assertTrue(out.startswith("? not json "))
        self.assertIsNone(LIVE.search(out))


class LogsTests(test_jobs.JobCase):
    def logs(self, **kw):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ab.cmd_logs(self.prof, self.args(**kw))
        return out.getvalue()

    def hostile_job(self):
        job = self.start()
        self.finish(job)
        with open(self.vm.jobdir(job) / "output.jsonl", "ab") as f:             # agent-writable, may hold raw bytes
            f.write(f"{OSC52}\xff raw {CSI8}\n".encode("latin-1") + b"\xff\xfe\x1b[2J broken utf-8\n")
        return job

    def test_raw_and_rendered_are_safe_and_dont_crash_on_bad_bytes(self):
        self.hostile_job()
        for raw in (True, False):
            out = self.logs(raw=raw)
            self.assertIsNone(LIVE.search(out.replace("\n", "")), repr(out))
        raw = self.logs(raw=True)
        self.assertIn("\\x1b]52;c;", raw)
        self.assertIn("�", raw)                                            # decoded with errors="replace"
        self.assertIn("[init] session sess-12345678", self.logs())


class DiagnosticsTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.on]\nworkspace = "{self.ws}"\n')

    def test_doctor_inside_sandbox_lines(self):
        hostile = f"{ESC}]0;pwned{BEL}hosttool=absent {CSI}"
        FakeSbx(exists=True, names=("ab-on",), exec_out={
            "TOKENVAR": (0, f"TOKENVAR A_KEY placeholder 3\n{hostile}\n"),
            "ssh-add": (2, "Could not open a connection to your authentication agent.\n")}).install(self)
        code, out, err = self.run_main("doctor", "on")
        self.assertIsNone(LIVE.search((out + err).replace("\n", "")))
        self.assertIn("\\x1b]0;pwned\\x07hosttool=absent", out)
        code, out, _ = self.run_main("doctor", "on", "--json")                 # json.dumps escapes it already
        self.assertIsNone(LIVE.search(out.replace("\n", "")))
        self.assertIn(hostile, [c["message"].removeprefix("inside sandbox: ") for c in json.loads(out)["checks"]])

    @needs_hostile_filenames
    def test_audit_file_names_and_git_config_values(self):
        prof = ab.Config().profile("on")
        (self.ws / ".git").mkdir()
        (self.ws / ".git" / "config").write_text("[core]\n")
        ab.write_snapshot(prof)
        (self.ws / f".env.{ESC}]52;c;AAAA{BEL}").write_text("x")
        (self.ws / ".git" / "config").write_text(f"[alias]\n    x = !echo {OSC52}\n")
        out = io.StringIO()
        with mock.patch.object(ab, "sandbox_git_status", lambda *a: None), contextlib.redirect_stdout(out):
            ab.cmd_audit(prof)
        self.assertIsNone(LIVE.search(out.getvalue().replace("\n", "")), repr(out.getvalue()))
        self.assertIn("\\x1b]52;c;AAAA\\x07", out.getvalue())
        self.assertIn("alias.x", out.getvalue())


if __name__ == "__main__":
    unittest.main()
