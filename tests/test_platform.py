"""The platform shim run for real (no stubs): permissions, process lookup, termination, detaching."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from helpers import ab

SLEEPER = "import time; time.sleep(60)  # agentbox-platform-test"


def spawn(**kw):
    return subprocess.Popen([sys.executable, "-c", SLEEPER], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kw)


def wait_gone(p, secs=10):
    end = time.time() + secs
    while time.time() < end:
        if p.poll() is not None:
            return True
        time.sleep(0.05)
    return False


class OwnerOnlyTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "icacls")
    def test_windows_acl_is_reduced_to_the_current_user(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "token"
            f.write_text("secret")
            ab.owner_only(f)
            acl = subprocess.run(["icacls", str(f)], capture_output=True, text=True).stdout
            self.assertIn(os.environ["USERNAME"].lower(), acl.lower())
            # No broad group may keep access. SYSTEM and Administrators can stay: an elevated token's default
            # DACL names them (CI runners are admin), and they can take ownership of the file anyway.
            for broad in ("Everyone", "BUILTIN\\Users", "Authenticated Users", "NT AUTHORITY\\INTERACTIVE"):
                self.assertNotIn(broad, acl)
            self.assertEqual(f.read_text(), "secret")          # we can still use it ourselves

    @unittest.skipUnless(sys.platform == "win32", "icacls")
    def test_windows_directory_acl_is_inherited_by_new_files(self):
        with tempfile.TemporaryDirectory() as d:
            ab.owner_only(d, is_dir=True)
            f = Path(d) / "new.txt"
            f.write_text("x")
            acl = subprocess.run(["icacls", str(f)], capture_output=True, text=True).stdout
            self.assertNotIn("Everyone", acl)
            self.assertNotIn("Authenticated Users", acl)

    @unittest.skipIf(sys.platform == "win32", "POSIX modes")
    def test_posix_modes(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "token"
            f.write_text("x")
            ab.owner_only(f)
            ab.owner_only(d, is_dir=True)
            self.assertEqual(f.stat().st_mode & 0o777, 0o600)
            self.assertEqual(Path(d).stat().st_mode & 0o777, 0o700)

    def test_owned_by_me(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(ab.owned_by_me(Path(d)))


class ProcessTests(unittest.TestCase):
    def test_process_cmdline_sees_a_running_process(self):
        p = spawn()
        try:
            self.assertIn("agentbox-platform-test", " ".join(ab.process_cmdline(p.pid)))
        finally:
            p.kill()
            p.wait()

    def test_process_cmdline_of_a_missing_process_is_empty(self):
        p = spawn()
        p.kill()
        p.wait()
        self.assertEqual(ab.process_cmdline(p.pid), [])

    def test_terminate_stops_a_process(self):
        p = spawn()
        try:
            ab.terminate(p.pid)
            self.assertTrue(wait_gone(p), "process still running after terminate()")
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()

    def test_detached_child_survives_and_can_be_terminated(self):
        p = spawn(**ab.detached_popen_kwargs())
        try:
            time.sleep(0.3)
            self.assertIsNone(p.poll())
            self.assertIn("agentbox-platform-test", " ".join(ab.process_cmdline(p.pid)))
            ab.terminate(p.pid)
            self.assertTrue(wait_gone(p))
        finally:
            if p.poll() is None:
                p.kill()
            p.wait()

    def test_pid_of_only_returns_our_own_processes(self):
        # a pid file naming an unrelated live process must be rejected and removed
        with tempfile.TemporaryDirectory() as d:
            pf = Path(d) / "bridge.pid"
            p = spawn()
            try:
                pf.write_text(str(p.pid))
                self.assertIsNone(ab._pid_of(pf, "bridge", "someprofile"))
                self.assertFalse(pf.exists())
            finally:
                p.kill()
                p.wait()


if __name__ == "__main__":
    unittest.main()
