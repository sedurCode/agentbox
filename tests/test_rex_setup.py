"""`agentbox rex setup|status|remove`: our marked block in ~/.config/rex/init.lua, under a temp HOME."""
import json
import os
from unittest import mock

from helpers import AgentboxTestCase, ab


class RexSetupTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        self.home.mkdir()
        p = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        p.start()
        self.addCleanup(p.stop)
        self.share = self.home / ".local/share/agentbox/share"
        (self.share / "rex").mkdir(parents=True)
        (self.share / "rex" / "agentbox.lua").write_text("return {}\n")
        p = mock.patch.object(ab, "SHARE_DIR", self.share)
        p.start()
        self.addCleanup(p.stop)
        self.init = self.home / ".config/rex/init.lua"

    def state(self):
        code, out, _ = self.run_main("rex", "status", "--json")
        self.assertEqual(code, 0)
        return json.loads(out)["state"]

    def test_setup_creates_init_lua_with_the_block(self):
        self.assertEqual(self.state(), "missing")
        self.assertEqual(self.run_main("rex", "setup")[0], 0)
        text = self.init.read_text()
        self.assertTrue(text.startswith(ab.REX_BEGIN))
        self.assertIn('require("agentbox").setup{}', text)
        if not ab.IS_WIN:
            self.assertIn('os.getenv("HOME") .. "/.local/share/agentbox/share/rex/?.lua;"', text)
        self.assertEqual(self.state(), "current")
        self.run_main("rex", "setup")                       # idempotent
        self.assertEqual(self.init.read_text(), text)

    def test_setup_appends_and_remove_restores_the_rest(self):
        self.init.parent.mkdir(parents=True)
        mine = "-- my config\nrex.font = 'x'"                # no trailing newline
        self.init.write_text(mine)
        if not ab.IS_WIN:
            os.chmod(self.init, 0o600)
        self.run_main("rex", "setup")
        text = self.init.read_text()
        self.assertTrue(text.startswith(mine + "\n\n" + ab.REX_BEGIN))
        if not ab.IS_WIN:
            self.assertEqual(self.init.stat().st_mode & 0o777, 0o600)
        self.init.write_text(text + "-- after\n")
        self.run_main("rex", "remove")
        self.assertEqual(self.init.read_text(), mine + "\n\n-- after\n")
        self.assertEqual(self.state(), "missing")

    def test_outdated_block_is_replaced_in_place(self):
        self.init.parent.mkdir(parents=True)
        old = ab.rex_block().replace("share/rex", "old/rex")
        self.init.write_text("a\n" + old + "b\n")
        self.assertEqual(self.state(), "outdated")
        self.run_main("rex", "setup")
        self.assertEqual(self.init.read_text(), "a\n" + ab.rex_block() + "b\n")

    def test_hand_written_lines_are_left_alone(self):
        self.init.parent.mkdir(parents=True)
        manual = 'package.path = "/x/?.lua;" .. package.path\nrequire("agentbox").setup{}\n'
        self.init.write_text(manual)
        self.assertEqual(self.state(), "manual")
        for action in ("setup", "remove"):
            code, out, _ = self.run_main("rex", action)
            self.assertEqual(code, 0)
            self.assertIn("left alone", out)
            self.assertEqual(self.init.read_text(), manual)

    def test_setup_needs_the_installed_actions(self):
        (self.share / "rex" / "agentbox.lua").unlink()
        code, _, err = self.run_main("rex", "setup")
        self.assertNotEqual(code, 0)
        self.assertIn("reinstall agentbox", err)
        self.assertFalse(self.init.exists())

    def test_json_only_for_status(self):
        self.assertEqual(self.run_main("rex", "setup", "--json")[0], 2)

    def test_orphan_begin_marker_never_takes_the_users_lines(self):
        # a begin marker whose end and require lines were deleted by hand, then setup appends a fresh block
        self.init.parent.mkdir(parents=True)
        mine = ab.REX_BEGIN + "\n-- my config\nrex.font = 'x'\n"
        self.init.write_text(mine)
        self.assertEqual(self.state(), "missing")
        self.run_main("rex", "setup")
        self.assertEqual(self.state(), "current")
        self.run_main("rex", "remove")
        self.assertEqual(self.init.read_text(), mine + "\n")

    def test_crlf_line_endings_are_kept(self):
        self.init.parent.mkdir(parents=True)
        self.init.write_bytes(b"-- mine\r\nrex.font = 'x'\r\n")
        self.run_main("rex", "setup")
        raw = self.init.read_bytes()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))       # no bare LF anywhere
        self.assertEqual(self.state(), "current")
        self.run_main("rex", "remove")
        self.assertEqual(self.init.read_bytes(), b"-- mine\r\nrex.font = 'x'\r\n\r\n")

    def test_symlinked_init_lua_is_written_through(self):
        if ab.IS_WIN:
            self.skipTest("symlinks need privileges on Windows")
        dots = self.home / "dotfiles"
        dots.mkdir()
        (dots / "init.lua").write_text("-- mine\n")
        self.init.parent.mkdir(parents=True)
        self.init.symlink_to(dots / "init.lua")
        self.run_main("rex", "setup")
        self.assertTrue(self.init.is_symlink())
        self.assertIn(ab.REX_BEGIN, (dots / "init.lua").read_text())

    def test_unreadable_init_lua_is_a_message_not_a_traceback(self):
        self.init.parent.mkdir(parents=True)
        self.init.write_bytes(b"-- caf\xe9\n")                    # Latin-1
        code, _, err = self.run_main("rex", "status")
        self.assertNotEqual(code, 0)
        self.assertIn("isn't UTF-8", err)
        self.assertNotIn("Traceback", err)
