"""The host-tool bridge on a Windows host: the agent sees /d/x, the host has D:\\x."""
import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from helpers import AgentboxTestCase, ab

WIN = sys.platform == "win32"


class HostPathTests(unittest.TestCase):
    """host_path() with IS_WIN forced on, so the mapping rules are checked on every platform."""

    def h(self, s):
        with mock.patch.object(ab, "IS_WIN", True):
            return ab.host_path(s).as_posix()

    def test_vm_paths_map_to_drive_paths(self):
        self.assertEqual(self.h("/d/Neural/zenaudiodsp"), "D:/Neural/zenaudiodsp")
        self.assertEqual(self.h("/C/Users/me"), "C:/Users/me")
        self.assertEqual(self.h("/e").rstrip("/"), "E:")      # the drive root

    def test_drive_and_relative_paths_pass_through(self):
        self.assertEqual(self.h("D:/x/y.elf"), "D:/x/y.elf")
        self.assertEqual(self.h("build/fw.elf"), "build/fw.elf")
        self.assertEqual(self.h("../x"), "../x")

    def test_other_absolute_posix_paths_are_refused(self):
        for bad in ("/", "/etc/passwd", "/tmp/x", "//server/share/x", "\\\\server\\share", "/dd/x", "/1/x"):
            with self.assertRaises(ValueError, msg=bad):
                self.h(bad)

    def test_posix_host_is_unchanged(self):
        with mock.patch.object(ab, "IS_WIN", False):
            self.assertEqual(ab.host_path("/etc/passwd").as_posix(), "/etc/passwd")
            self.assertEqual(ab.host_path("a/b").as_posix(), "a/b")


class ToolEnvTests(unittest.TestCase):
    def test_windows_env_has_what_tools_need_and_no_secrets(self):
        env_in = {"SystemRoot": "C:/Windows", "TEMP": "C:/T", "GH_TOKEN": "x", "ANTHROPIC_API_KEY": "y",
                  "SSH_AUTH_SOCK": "z", "PATH": "C:/secretbin"}
        with mock.patch.object(ab, "IS_WIN", True), mock.patch.dict(os.environ, env_in, clear=True):
            env = ab.tool_base_env()
        self.assertEqual(env["SystemRoot"], "C:/Windows")
        self.assertEqual(env["TEMP"], "C:/T")
        self.assertIn(".EXE", env["PATHEXT"])
        for k in ("GH_TOKEN", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK"):
            self.assertNotIn(k, env)
        self.assertNotIn("secretbin", env["PATH"])         # the user's PATH is not inherited

    def test_posix_env_is_unchanged(self):
        with mock.patch.object(ab, "IS_WIN", False):
            env = ab.tool_base_env()
        self.assertEqual(set(env), {"PATH", "HOME", "LANG"})
        self.assertEqual(env["PATH"], ab.HOST_TOOL_PATH)    # the Linux-aware list, not a stale macOS one


@unittest.skipUnless(WIN, "runs real Windows processes")
class RunToolTests(unittest.TestCase):
    def test_runs_and_captures(self):
        rc, out, err = ab.run_tool([sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('e'); sys.exit(3)"],
                                   None, ab.tool_base_env() | {"PATH": os.environ["PATH"]}, 30)
        self.assertEqual((rc, out.strip(), err.strip()), (3, b"hi", b"e"))

    def test_timeout_kills_the_whole_process_tree(self):
        marker = "agentbox-grandchild-test"
        script = textwrap.dedent(f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)  # {marker}"])
            time.sleep(60)
        """)
        t0 = time.time()
        rc, out, err = ab.run_tool([sys.executable, "-c", script], None, dict(os.environ), 3)
        self.assertEqual(rc, 124)
        self.assertIn(b"timed out", err)
        self.assertLess(time.time() - t0, 30)
        time.sleep(0.5)
        ps = (f"@(Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -like '*{marker}*' "
              f"-and $_.Name -like 'python*' }}).Count")
        left = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True).stdout.strip()
        self.assertEqual(left, "0", "a grandchild survived the timeout")


class BridgeWindowsTests(AgentboxTestCase):
    def bridge(self, argv, extra=""):
        cfg = self.write_config(f'''
            [tools.t]
            argv = {argv!r}
            params.elf = {{ type = "path", suffixes = [".elf"] }}
            {extra}
            [profiles.p]
            workspace = "{self.ws}"
            host_tools = ["t"]
            host_tools_port = 47998
        '''.replace("'", '"'))
        return ab.Bridge(cfg.profile("p"))

    @unittest.skipUnless(WIN, "needs real Windows paths")
    def test_vm_cwd_and_paths_are_mapped_and_still_confined(self):
        (self.ws / "fw.elf").write_text("x")
        b = self.bridge([sys.executable, "{elf}"])
        vm_ws = ab.vm_path(self.ws)
        for given in ("fw.elf", f"{vm_ws}/fw.elf", str(self.ws / "fw.elf")):
            _, argv, _ = b.build("t", {"elf": given}, [], vm_ws)
            self.assertEqual(Path(argv[1]), (self.ws / "fw.elf").resolve(), given)
        for bad in ("/etc/passwd.elf", "/c/Windows/win.elf", f"{vm_ws}/../outside.elf", "//srv/share/x.elf"):
            with self.assertRaises(ab.ToolError, msg=bad):
                b.build("t", {"elf": bad}, [], vm_ws)
        with self.assertRaises(ab.ToolError):                 # cwd outside every workspace
            b.build("t", {"elf": "fw.elf"}, [], "/c/Windows")
        with self.assertRaises(ab.ToolError):                 # cwd that is not a drive path at all
            b.build("t", {"elf": "fw.elf"}, [], "/")

    def test_batch_files_are_refused_on_windows(self):
        bat = self.tmp / "tools" / "tool.bat"
        bat.parent.mkdir()
        bat.write_text("@echo hi")
        # IS_WIN is forced on a POSIX host too, so keep unrelated Windows-only steps (icacls) out of it
        with mock.patch.object(ab, "IS_WIN", True), mock.patch.object(ab, "owner_only", lambda *a, **k: None), \
                self.assertRaises(SystemExit):
            self.bridge([str(bat)])

    @unittest.skipUnless(WIN, "needs real Windows paths")
    def test_exe_suffix_is_optional(self):
        exe = Path(sys.executable)
        b = self.bridge([str(exe.with_suffix(""))])           # ...\python, not ...\python.exe
        self.assertEqual(ab.resolve_exe(Path(b.tools["t"]["argv"][0])).suffix.lower(), ".exe")

    def test_confirm_windows_passes_the_command_through_the_environment_and_denies_by_default(self):
        b = self.bridge([sys.executable])
        calls = []

        def fake(argv, cwd, env, timeout):
            calls.append((argv, env))
            return fake.result

        evil = ["C:/x/tool.exe", "'; Stop-Computer; '", '"quoted"']
        with mock.patch.object(ab, "run_tool", fake), mock.patch.object(ab, "IS_WIN", True):
            for result, want in (((0, b"Yes", b""), True), ((0, b"No", b""), False), ((1, b"Yes", b""), False),
                                 ((124, b"", b"timed out"), False), ((0, b"", b""), False)):
                fake.result = result
                self.assertEqual(b.confirm("t", evil, 5), want, result)
        argv, env = calls[0]
        self.assertEqual(argv[0], "powershell")
        script = argv[-1]
        self.assertNotIn("Stop-Computer", script)             # the agent-influenced text is never in the script
        self.assertIn("Stop-Computer", env["AGENTBOX_CONFIRM_MSG"])
        self.assertIn("'Button2'", script)                    # the default button is No


if __name__ == "__main__":
    unittest.main()
