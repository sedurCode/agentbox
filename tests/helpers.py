"""Shared test setup: load bin/agentbox (no .py suffix) and keep tests off the real sbx."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
REAL_RUN, REAL_POPEN = subprocess.run, subprocess.Popen   # for the few tests that run `sh` on purpose


@contextlib.contextmanager
def real_sh():
    """Allow subprocess for one block (used only to run the in-VM probe under plain `sh`)."""
    with mock.patch.object(subprocess, "Popen", REAL_POPEN):
        yield


def load_agentbox():
    loader = importlib.machinery.SourceFileLoader("agentbox_under_test", str(ROOT / "bin" / "agentbox"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


ab = load_agentbox()


def _forbidden(what):
    def fail(*a, **kw):
        raise AssertionError(f"test attempted a real {what} call: {a!r}")
    return fail


class AgentboxTestCase(unittest.TestCase):
    """Temp config dir + state dir; sbx and subprocess calls fail loudly unless a test stubs them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.cfg_dir = self.tmp / "cfg"
        self.cfg_dir.mkdir()
        self.state_dir = self.tmp / "state"
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        for name, val in (("CONFIG_DIR", self.cfg_dir), ("CONFIG_FILE", self.cfg_dir / "agentbox.toml"),
                          ("KITS_DIR", self.cfg_dir / "kits")):
            p = mock.patch.object(ab, name, val)
            p.start()
            self.addCleanup(p.stop)
        for target, what in (("sbx", "sbx"), ("sandbox_exists", "sbx")):
            p = mock.patch.object(ab, target, _forbidden(what))
            p.start()
            self.addCleanup(p.stop)
        for name in ("run", "Popen", "check_output"):
            p = mock.patch.object(subprocess, name, _forbidden(f"subprocess.{name}"))
            p.start()
            self.addCleanup(p.stop)
        ab._WARNED_TOOLS.clear()

    def write_config(self, body: str) -> "ab.Config":
        header = f'[settings]\nsbx = "sbx-must-not-run"\nstate_dir = "{self.state_dir}"\n'
        (self.cfg_dir / "agentbox.toml").write_text(header + textwrap.dedent(body))
        return ab.Config()

    def run_main(self, *argv):
        """Run main() with argv; returns (exit_code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        old_umask = os.umask(0o022)         # main() sets a restrictive umask; don't leak it
        self.addCleanup(os.umask, old_umask)
        with mock.patch.object(sys, "argv", ["agentbox", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ab.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()


class FakeSbx:
    """Stand-in for ab.sbx. `exec_out` maps a marker found in the exec command to (rc, stdout);
    anything unexpected fails the test."""

    def __init__(self, exists=False, exec_out=None):
        self.exists, self.exec_out, self.calls = exists, exec_out or {}, []

    def __call__(self, cfg, *args, check=True, capture=False, stdin=None):
        self.calls.append(args)
        done = lambda rc=0, out="": subprocess.CompletedProcess(args, rc, out, "")
        if args[0] == "version":
            return done(0, "sbx 9.9.9\n")
        if args == ("ls",):
            return done(0, "NAME STATUS\n")
        if args[:2] == ("kit", "validate"):
            return done()
        if args[0] == "exec":
            for marker, (rc, out) in self.exec_out.items():
                if marker in args[-1]:
                    return done(rc, out)
        raise AssertionError(f"unexpected sbx call: {args!r}")

    def install(self, case):
        for name, fn in (("sbx", self), ("sandbox_exists", lambda prof: self.exists)):
            p = mock.patch.object(ab, name, fn)
            p.start()
            case.addCleanup(p.stop)
        return self
