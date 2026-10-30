"""Shared test setup: load bin/agentbox (no .py suffix) and keep tests off the real sbx."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
REAL_KILL, REAL_KILLPG = os.kill, os.killpg
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
        # A test once SIGTERMed PID 1 (the VM's init) and took the sandbox down. Only processes
        # a test registered with allow_kill() can be signalled; anything else fails loudly.
        self.killable = set()
        for mod, name in ((os, "kill"), (os, "killpg")):
            p = mock.patch.object(mod, name, self._guarded(name))
            p.start()
            self.addCleanup(p.stop)

    def _guarded(self, name):
        real = REAL_KILL if name == "kill" else REAL_KILLPG

        def guard(pid, sig):
            if not isinstance(pid, int) or pid <= 1 or pid not in self.killable:
                raise AssertionError(f"test tried os.{name}({pid!r}, {sig!r}) on a process it did not start")
            return real(pid, sig)
        return guard

    def allow_kill(self, pid: int):
        self.killable.add(pid)

    def spawn_sleep(self, seconds: int = 30):
        """A harmless process we own, for tests that need a live pid that is NOT a job wrapper."""
        proc = REAL_POPEN(["sleep", str(seconds)])
        self.allow_kill(proc.pid)
        self.addCleanup(lambda: (proc.poll() is None and proc.terminate(), proc.wait()))
        return proc

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

    def __init__(self, exists=False, exec_out=None, vm="running", names=("ab-on", "ab-plain", "ab-p"), boot=False):
        self.exists, self.exec_out, self.calls = exists, exec_out or {}, []
        self.vm, self.names, self.boot = vm, names, boot      # boot: an exec may start a stopped VM

    def __call__(self, cfg, *args, check=True, capture=False, stdin=None):
        self.calls.append(args)
        done = lambda rc=0, out="": subprocess.CompletedProcess(args, rc, out, "")
        if args[0] == "version":
            return done(0, "sbx 9.9.9\n")
        if args == ("ls",):
            return done(0, "NAME STATUS\n")
        if args == ("ls", "--json"):
            return done(0, json.dumps([{"name": n, "status": self.vm} for n in self.names] if self.exists else []))
        if args[0] == "exec" and self.vm != "running":
            if not self.boot:
                raise AssertionError(f"exec into a {self.vm} sandbox would boot it: {args!r}")
            self.vm = "running"
        if args[:2] == ("kit", "validate"):
            return done()
        if args[0] == "exec":
            for marker, (rc, out) in self.exec_out.items():
                if marker in args[-1]:
                    return done(rc, out)
        raise AssertionError(f"unexpected sbx call: {args!r}")

    def install(self, case):
        for name, fn in (("sbx", self), ("sandbox_exists", lambda prof, retry=False: self.exists)):
            p = mock.patch.object(ab, name, fn)
            p.start()
            case.addCleanup(p.stop)
        return self


FAKE_CLAUDE = """#!/bin/sh
cat > /dev/null
echo '{"type":"system","subtype":"init","session_id":"sess-12345678","model":"m"}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"hello there"},{"type":"tool_use","name":"Bash","input":{"command":"ls"}}]}}'
sleep ${FAKE_CLAUDE_SLEEP:-0}
echo '{"type":"result","subtype":"success","result":"all done"}'
exit ${FAKE_CLAUDE_EXIT:-0}
"""


class LocalVM:
    """Stands in for sbx by running `exec ... sh -c SCRIPT` on THIS machine with HOME pointed at a
    temp dir and a fake `claude` on PATH, so the job scripts really run. Nothing else is allowed."""

    def __init__(self, case, exists=True, vm="running"):
        self.case, self.exists, self.vm, self.calls, self.stdins = case, exists, vm, [], []
        self.ls_fail = self.boot = False       # ls_fail: the daemon is unreachable; boot: exec may start a stopped VM
        self.home = case.tmp / "vmhome"
        self.home.mkdir()
        bindir = case.tmp / "vmbin"
        bindir.mkdir()
        (bindir / "claude").write_text(FAKE_CLAUDE)
        (bindir / "claude").chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home), "PATH": f"{bindir}:{os.environ['PATH']}"}
        for name, fn in (("sbx", self), ("sandbox_exists", lambda prof, retry=False: self.exists)):
            p = mock.patch.object(ab, name, fn)
            p.start()
            case.addCleanup(p.stop)
        case.addCleanup(self.kill_jobs)

    def jobdir(self, job_id):
        return self.home / ".agentbox" / "jobs" / job_id

    def kill_jobs(self):
        """Cleanup: stop leftover fake agents through the same validated killer `down --force` uses."""
        for jobdir in self.home.glob(".agentbox/jobs/*"):
            if ab.JOB_ID_RE.match(jobdir.name):
                with mock.patch.object(subprocess, "Popen", REAL_POPEN):
                    REAL_RUN(["sh", "-c", ab.kill_script(jobdir.name)], env=self.env, capture_output=True)

    def __call__(self, cfg, *args, check=True, capture=False, stdin=None):
        self.calls.append(args)
        self.stdins.append(stdin)
        done = lambda rc=0, out="", err="": subprocess.CompletedProcess(args, rc, out, err)
        if args[0] == "inspect":
            return done(0 if self.exists else 1)
        if args == ("ls", "--json"):
            if self.ls_fail:
                return done(1, "", "error: context deadline exceeded")
            return done(0, json.dumps([{"name": "ab-p", "status": self.vm}] if self.exists else []))
        if args[0] == "exec" and self.vm != "running":
            if not self.boot:
                raise AssertionError(f"exec into a {self.vm} sandbox would boot it: {args!r}")
            self.vm = "running"
        if args[0] == "rm":
            return done()
        if args[0] == "exec" and args[-3:-1] == ("sh", "-c"):
            with mock.patch.object(subprocess, "Popen", REAL_POPEN):
                r = REAL_RUN(["sh", "-c", args[-1]], env=self.env, input=stdin, capture_output=True, text=True,
                             errors="replace")
            return done(r.returncode, r.stdout, r.stderr)
        raise AssertionError(f"unexpected sbx call: {args!r}")
