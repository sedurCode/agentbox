"""Everything agentbox sends into the Linux VM must keep LF line endings, also from a Windows host."""
import subprocess
import unittest
from unittest import mock

from helpers import AgentboxTestCase, ab, load_agentbox


class NewlineTests(AgentboxTestCase):
    def test_sbx_stdin_is_sent_as_bytes_with_lf(self):
        cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        done = subprocess.CompletedProcess([], 0, b"out\r\nline\r\n", b"")
        real = load_agentbox()          # the shared module's sbx() is stubbed out by the harness
        with mock.patch.object(subprocess, "run", return_value=done) as run:
            r = real.sbx(cfg, "exec", "x", "sh", check=False, capture=True, stdin="#!/bin/sh\necho hi\n")
        kw = run.call_args.kwargs
        self.assertEqual(kw["input"], b"#!/bin/sh\necho hi\n")      # no text mode: nothing can add a \r
        self.assertNotIn("text", kw)
        self.assertEqual(r.stdout, "out\nline\n")                    # CRLF from the host side is normalised

    def test_generated_kit_files_have_no_carriage_returns(self):
        cfg = self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\nallow = ["api.example.com"]\n')
        prof = cfg.profile("p")
        kit_dir, _ = ab.render_local_kit(prof, "tok-123")
        from pathlib import Path
        for f in Path(kit_dir).rglob("*"):
            if f.is_file():
                self.assertNotIn(b"\r", f.read_bytes(), f"{f} has CR characters")


if __name__ == "__main__":
    unittest.main()
