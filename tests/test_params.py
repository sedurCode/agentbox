import os
import sys
import unittest

from helpers import AgentboxTestCase, ab


class HostToolParamTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "secret.elf").write_text("x")
        (self.ws / "fw.elf").write_text("x")
        (self.ws / "notes.txt").write_text("x")
        (self.ws / "sub").mkdir()
        os.symlink(self.outside / "secret.elf", self.ws / "link.elf")      # symlink escape
        os.symlink(self.outside, self.ws / "linkdir")
        cfg = self.write_config(f'''
            [tools.flash]
            argv = ["{sys.executable}", "{{elf}}"]
            params.elf = {{ type = "path", suffixes = [".elf"] }}
            [tools.mem]
            argv = ["{sys.executable}", "{{addr}}", "{{words}}"]
            params.addr = {{ type = "int", format = "hex", span = {{ param = "words", scale = 4 }}, ranges = [
              {{ name = "RAM", start = 0x20000000, end = 0x2000FFFF }}, [0x08000000, 0x0800FFFF] ] }}
            params.words = {{ type = "int", min = 1, max = 256, default = 16 }}
            [tools.pick]
            argv = ["{sys.executable}", "{{mode}}", "{{name}}"]
            params.mode = {{ type = "enum", values = ["fast", "slow"] }}
            params.name = {{ type = "string", pattern = "[a-z-]{{1,8}}" }}
            [tools.free]
            argv = ["{sys.executable}"]
            extra_args = {{ pattern = "[a-z0-9=-]{{1,10}}", max = 2, deny_prefixes = ["--bad"] }}
            [tools.noextra]
            argv = ["{sys.executable}"]
            [profiles.p]
            workspace = "{self.ws}"
            host_tools = ["flash", "mem", "pick", "free", "noextra"]
            host_tools_port = 47998
        ''')
        self.bridge = ab.Bridge(cfg.profile("p"))

    def build(self, tool, params=None, extra=None, cwd=None):
        return self.bridge.build(tool, params or {}, extra or [], str(cwd or self.ws))

    def assertRejected(self, *a, **kw):
        with self.assertRaises(ab.ToolError):
            self.build(*a, **kw)

    # ---- path
    def test_path_inside_workspace(self):
        _, argv, _ = self.build("flash", {"elf": "fw.elf"})
        self.assertEqual(argv[-1], str(self.ws / "fw.elf"))
        _, argv, _ = self.build("flash", {"elf": str(self.ws / "fw.elf")})     # absolute works too
        self.assertEqual(argv[-1], str(self.ws / "fw.elf"))

    def test_path_outside_workspace_rejected(self):
        self.assertRejected("flash", {"elf": str(self.outside / "secret.elf")})
        self.assertRejected("flash", {"elf": "../outside/secret.elf"})

    def test_path_symlink_escape_rejected(self):
        self.assertRejected("flash", {"elf": "link.elf"})
        self.assertRejected("flash", {"elf": "linkdir/secret.elf"})

    def test_path_suffix_missing_and_directory(self):
        self.assertRejected("flash", {"elf": "notes.txt"})                      # wrong suffix
        self.assertRejected("flash", {"elf": "absent.elf"})                     # must exist
        self.assertRejected("flash", {"elf": "sub"})                            # not a file
        self.assertRejected("flash", {})                                        # required

    def test_cwd_must_be_in_a_workspace(self):
        self.assertRejected("flash", {"elf": "fw.elf"}, cwd=self.outside)

    # ---- int
    def test_int_ranges_with_span(self):
        _, argv, _ = self.build("mem", {"addr": "0x20000000", "words": "64"})
        self.assertEqual(argv[-2:], ["0x20000000", "64"])
        _, argv, _ = self.build("mem", {"addr": "0x20000000"})                   # default words=16
        self.assertEqual(argv[-1], "16")
        # last word exactly at the end of the range is fine, one past is not
        self.build("mem", {"addr": "0x2000FFFC", "words": "1"})
        self.assertRejected("mem", {"addr": "0x2000FFFC", "words": "2"})
        # starts in RAM, runs into the gap after it
        self.assertRejected("mem", {"addr": "0x2000FF00", "words": "256"})
        self.assertRejected("mem", {"addr": "0x40000000", "words": "1"})         # peripheral space
        self.build("mem", {"addr": "0x08000000", "words": "4"})                  # list-style range

    def test_int_min_max_and_garbage(self):
        self.assertRejected("mem", {"addr": "0x20000000", "words": "0"})
        self.assertRejected("mem", {"addr": "0x20000000", "words": "257"})
        self.assertRejected("mem", {"addr": "0x20000000", "words": "lots"})
        self.assertRejected("mem", {"addr": "0x20000000", "words": [1]})

    # ---- enum / string
    def test_enum(self):
        self.build("pick", {"mode": "fast", "name": "ok"})
        self.assertRejected("pick", {"mode": "medium", "name": "ok"})

    def test_string_pattern_and_leading_dash(self):
        self.assertRejected("pick", {"mode": "fast", "name": "UPPER"})
        self.assertRejected("pick", {"mode": "fast", "name": "toolongname"})
        self.assertRejected("pick", {"mode": "fast", "name": "-rf"})             # matches pattern, leading dash
        self.assertRejected("pick", {"mode": "fast", "name": "ok; id"})

    def test_unknown_param_and_tool(self):
        self.assertRejected("pick", {"mode": "fast", "name": "ok", "extra": "1"})
        self.assertRejected("nosuchtool")

    # ---- extra_args
    def test_extra_args(self):
        _, argv, _ = self.build("free", extra=["a=1", "b"])
        self.assertEqual(argv[1:], ["a=1", "b"])
        self.assertRejected("free", extra=["a", "b", "c"])                       # too many
        self.assertRejected("free", extra=["Bad!"])                              # pattern
        self.assertRejected("free", extra=["--bad"])                             # matches the pattern, hits deny_prefixes
        self.assertRejected("noextra", extra=["x"])                              # tool takes none

    def test_default_cwd_is_outside_every_workspace(self):
        _, _, run_cwd = self.build("noextra")
        self.assertFalse(ab.is_within(ab.Path(run_cwd).resolve(), self.ws))


class PreflightTests(AgentboxTestCase):
    def prof(self, argv):
        cfg = self.write_config(f'''
            [tools.t]
            argv = {argv!r}
            [profiles.p]
            workspace = "{self.ws}"
            host_tools = ["t"]
            host_tools_port = 47997
        '''.replace("'", '"'))
        return cfg.profile("p")

    def test_relative_executable_refused(self):
        with self.assertRaises(SystemExit):
            ab.Bridge(self.prof(["make"]))

    def test_executable_inside_workspace_refused(self):
        exe = self.ws / "tool.sh"
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
        with self.assertRaises(SystemExit):
            ab.Bridge(self.prof([str(exe)]))

    def test_fixed_path_arg_inside_workspace_refused(self):
        with self.assertRaises(SystemExit):
            ab.Bridge(self.prof([sys.executable, str(self.ws / "x.cfg")]))

    def test_missing_executable_warns_once_per_run(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ab.Bridge(self.prof(["/nonexistent/tool"]))
            ab.Bridge(self.prof(["/nonexistent/tool"]))
        self.assertEqual(err.getvalue().count("not executable/present"), 1)


if __name__ == "__main__":
    unittest.main()
