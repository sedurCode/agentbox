import argparse
import contextlib
import io
import json
import os
import tarfile
import unittest
import urllib.error
from unittest import mock

from helpers import AgentboxTestCase, ab

SHA = "0123456789abcdef0123456789abcdef01234567"
SRC = f"github:acme/skills-repo@{SHA}"
TOP = f"acme-skills-repo-{SHA}"


def member(name, data=b"", mode=0o644, type_=tarfile.REGTYPE, linkname=""):
    return dict(name=name, data=data, mode=mode, type=type_, linkname=linkname)


def make_tar(members, top=TOP):
    """tar.gz bytes. Names are used verbatim when they start with '!' (to build hostile ones)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for m in members:
            name = m["name"][1:] if m["name"].startswith("!") else f"{top}/{m['name']}"
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.linkname = m["type"], m["mode"], m["linkname"]
            if m["type"] == tarfile.REGTYPE:
                ti.size = len(m["data"])
                tf.addfile(ti, io.BytesIO(m["data"]))
            else:
                tf.addfile(ti)
    return buf.getvalue()


def good_members(extra=()):
    return [member("LICENSE", b"MIT License\n"),
            member("skills/", type_=tarfile.DIRTYPE, mode=0o755),
            member("skills/alpha/SKILL.md", b"# alpha\nDo it.\n"),
            member("skills/alpha/ref/notes.txt", b"notes\n"),
            member("skills/group/beta/SKILL.md", b"# beta\n"),
            member("skills/other/SKILL.md", b"# not selected\n"),
            *extra]


class VendorCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.into = self.tmp / "agent-skills"

    def args(self, only="alpha,group/beta", **kw):
        base = dict(source=SRC, only=only, into=str(self.into), prefix="skills/", text_only=True, yes=False)
        return argparse.Namespace(**{**base, **kw})

    def vendor(self, members=None, ask=None, expect=0, today="2026-10-31", **kw):
        data = make_tar(members if members is not None else good_members())
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = ab.cmd_skills_vendor(self.args(**kw), fetch=lambda o, r, s: data, ask=ask, today=today)
            except SystemExit as e:
                code = e.code
        if expect is not None:
            self.assertEqual(code, expect, err.getvalue() + out.getvalue())
        return out.getvalue(), err.getvalue()


class SourceTests(VendorCase):
    def test_parse(self):
        self.assertEqual(ab.parse_vendor_source(SRC), ("acme", "skills-repo", SHA))
        self.assertEqual(ab.parse_vendor_source(SRC.upper().replace("GITHUB", "github"))[2], SHA)   # hex is case-insensitive

    def test_branches_tags_and_short_shas_are_refused(self):
        for bad in ("github:acme/r@main", "github:acme/r@v1.2.3", "github:acme/r@abc1234", f"github:acme/r@{SHA}0",
                    f"github:acme/r@{SHA[:-1]}g", "github:acme/r", f"gitlab:acme/r@{SHA}", f"acme/r@{SHA}",
                    f"github:acme/../x@{SHA}", f"github:acme/..@{SHA}", f"github:-a/r@{SHA}", f"github:a/b/c@{SHA}"):
            with self.assertRaises(SystemExit, msg=bad) as cm:
                ab.parse_vendor_source(bad)
            self.assertEqual(cm.exception.code, 2, bad)

    def test_selection(self):
        self.assertEqual(ab.vendor_selection("skills/", "alpha, group/beta"),
                         {"alpha": "skills/alpha", "beta": "skills/group/beta"})
        self.assertEqual(ab.vendor_selection("", "x/y"), {"y": "x/y"})
        for bad in ("simplify", "a,a", "x/a,a", "Bad", "../x", "/abs", "a//b/..", "", "a,", "grp,grp/sub"):
            with self.assertRaises(SystemExit, msg=bad):
                ab.vendor_selection("skills/", bad)
        with self.assertRaises(SystemExit):
            ab.vendor_selection("../up", "a")


class ExtractTests(VendorCase):
    def test_extracts_only_the_selection_and_strips_the_top_folder(self):
        self.vendor()
        self.assertEqual(sorted(p.name for p in self.into.iterdir()), ["LICENSES", "SOURCES.json", "alpha", "beta"])
        self.assertEqual((self.into / "alpha" / "SKILL.md").read_text(), "# alpha\nDo it.\n")
        self.assertEqual((self.into / "alpha" / "ref" / "notes.txt").read_text(), "notes\n")
        self.assertEqual((self.into / "beta" / "SKILL.md").read_text(), "# beta\n")     # name = last component
        self.assertFalse((self.into / "other").exists())

    def test_modes_do_not_depend_on_the_umask(self):
        old = os.umask(0o077)
        try:
            self.vendor()
        finally:
            os.umask(old)
        self.assertEqual((self.into / "alpha" / "SKILL.md").stat().st_mode & 0o777, 0o644)
        self.assertEqual((self.into / "alpha" / "ref").stat().st_mode & 0o777, 0o755)

    HOSTILE = {
        "absolute path": [member("!/etc/cron.d/x", b"x")],
        "dotdot": [member("!" + TOP + "/skills/alpha/../../../x", b"x")],
        "dotdot leading": [member("!../x", b"x")],
        "backslash": [member("!" + TOP + "\\skills\\alpha\\SKILL.md", b"x")],
        "second top-level folder": [member("!other-top/skills/alpha/SKILL.md", b"x")],
        "git folder": [member("skills/alpha/.git/config", b"[core]\nfsmonitor = x\n")],
        "git folder, odd case": [member("skills/alpha/.GIT/config", b"x")],
        "symlink": [member("skills/alpha/link", type_=tarfile.SYMTYPE, linkname="/etc/passwd")],
        "hardlink": [member("skills/alpha/hard", type_=tarfile.LNKTYPE, linkname=TOP + "/skills/alpha/SKILL.md")],
        "device": [member("skills/alpha/dev", type_=tarfile.CHRTYPE)],
        "block device": [member("skills/alpha/blk", type_=tarfile.BLKTYPE)],
        "fifo": [member("skills/alpha/pipe", type_=tarfile.FIFOTYPE)],
        "skill path is a file": [member("skills/group/beta", b"x")],
    }

    def test_every_hostile_archive_is_refused_and_nothing_is_written(self):
        for label, extra in self.HOSTILE.items():
            members = good_members() + extra
            if label == "skill path is a file":
                members = [m for m in members if not m["name"].startswith("skills/group")] + extra
            with self.subTest(label):
                _, err = self.vendor(members, expect=1)
                self.assertIn("refusing archive", err)
                self.assertFalse(self.into.exists() and any(self.into.iterdir()), label)

    def test_hostile_paths_are_refused_even_outside_the_selection(self):
        _, err = self.vendor(good_members([member("!/etc/passwd", b"x")]), only="alpha", expect=1)
        self.assertIn("unsafe path", err)

    def test_links_outside_the_selection_are_ignored_because_they_are_never_read(self):
        extra = [member("skills/other/link", type_=tarfile.SYMTYPE, linkname="/etc/passwd"),
                 member("docs/dev", type_=tarfile.CHRTYPE)]
        self.vendor(good_members(extra), only="alpha")
        self.assertTrue((self.into / "alpha" / "SKILL.md").is_file())

    def test_missing_skill_or_skill_md_and_junk_input(self):
        _, err = self.vendor(good_members(), only="nope", expect=1)
        self.assertIn("not in the archive", err)
        _, err = self.vendor([member("skills/alpha/readme.md", b"x")], only="alpha", expect=1)
        self.assertIn("no SKILL.md", err)
        data = b"this is not a tarball"
        with self.assertRaises(SystemExit):
            ab.extract_vendor(data, {"alpha": "skills/alpha"}, True)
        with self.assertRaises(SystemExit):
            ab.extract_vendor(make_tar([]), {"alpha": "skills/alpha"}, True)

    def test_case_collisions_and_file_directory_conflicts(self):
        _, err = self.vendor(good_members([member("skills/alpha/skill.md", b"x")]), only="alpha", expect=1)
        self.assertIn("differ only in case", err)
        _, err = self.vendor(good_members([member("skills/alpha/ref", b"x")]), only="alpha", text_only=False, expect=1)
        self.assertIn("also a file", err)

    def test_decompression_bomb_is_stopped(self):
        big = make_tar(good_members([member("skills/alpha/big.txt", b"\0" * 200_000)]))
        with mock.patch.object(ab, "VENDOR_MAX_UNPACKED", 50_000), self.assertRaises(SystemExit):
            ab.extract_vendor(big, {"alpha": "skills/alpha"}, True)

    def test_selected_size_cap(self):
        with mock.patch.object(ab, "VENDOR_MAX_SELECTED", 10), self.assertRaises(SystemExit):
            ab.extract_vendor(make_tar(good_members()), {"alpha": "skills/alpha"}, True)


class TextOnlyTests(VendorCase):
    EXTRA = [member("skills/alpha/run.sh", b"#!/bin/sh\n", mode=0o755),
             member("skills/alpha/tool.py", b"print(1)\n"),
             member("skills/alpha/pic.png", b"\x89PNG"),
             member("skills/alpha/exec.md", b"# x\n", mode=0o755),
             member("skills/alpha/.gitattributes", b"* filter=x\n"),
             member("skills/alpha/data.JSON", b"{}"),
             member("skills/alpha/cfg.yml", b"a: 1\n"),
             member("skills/alpha/cfg.yaml", b"a: 1\n"),
             member("skills/alpha/LICENSE", b"MIT\n")]

    def test_default_drops_and_lists(self):
        _, err = self.vendor(good_members(self.EXTRA), only="alpha")
        left = sorted(str(p.relative_to(self.into / "alpha")) for p in (self.into / "alpha").rglob("*") if p.is_file())
        self.assertEqual(left, ["LICENSE", "SKILL.md", "cfg.yaml", "cfg.yml", "data.JSON", "ref/notes.txt"])
        for dropped in ("alpha/run.sh (executable)", "alpha/tool.py (not a text file)", "alpha/pic.png",
                        "alpha/exec.md (executable)", "alpha/.gitattributes"):
            self.assertIn(dropped, err)

    def test_the_result_passes_the_skill_set_rules(self):
        self.vendor(good_members(self.EXTRA), only="alpha,group/beta")
        cfg = self.write_config(f'[skillsets.core]\npath = "{self.into}"\n[profiles.p]\nworkspace = "{self.ws}"\n')
        ss = ab.inspect_skillset(cfg, "core")
        self.assertEqual(ss.problems, [])
        self.assertEqual(ss.skills, ["alpha", "beta"])          # SOURCES.json and LICENSES/ are not skills

    def test_no_text_only_keeps_scripts_and_the_exec_bit_but_never_git_files(self):
        _, err = self.vendor(good_members(self.EXTRA), only="alpha", text_only=False)
        self.assertTrue((self.into / "alpha" / "tool.py").is_file())
        self.assertEqual((self.into / "alpha" / "run.sh").stat().st_mode & 0o777, 0o755)
        self.assertFalse((self.into / "alpha" / ".gitattributes").exists())
        self.assertIn(".gitattributes", err)
        cfg = self.write_config(f'[skillsets.core]\npath = "{self.into}"\n[profiles.p]\nworkspace = "{self.ws}"\n')
        self.assertIn("not plain text", " ".join(ab.inspect_skillset(cfg, "core").problems))   # needs allow_scripts = true

    def test_skill_md_dropped_for_being_executable_is_an_error(self):
        members = [member("skills/alpha/SKILL.md", b"# x\n", mode=0o755)]
        _, err = self.vendor(members, only="alpha", expect=1)
        self.assertIn("no SKILL.md after dropping", err)


class SourcesAndLicenseTests(VendorCase):
    def test_sources_json(self):
        self.vendor()
        doc = json.loads((self.into / "SOURCES.json").read_text())
        alpha = doc["skills"]["alpha"]
        self.assertEqual((alpha["repo"], alpha["sha"], alpha["path"], alpha["vendored"]),
                         ("acme/skills-repo", SHA, "skills/alpha", "2026-10-31"))
        self.assertEqual(alpha["files"], {"SKILL.md": ab.hashlib.sha256(b"# alpha\nDo it.\n").hexdigest(),
                                          "ref/notes.txt": ab.hashlib.sha256(b"notes\n").hexdigest()})
        self.assertEqual(doc["skills"]["beta"]["path"], "skills/group/beta")

    def test_a_second_vendor_merges_and_a_corrupt_file_is_never_clobbered(self):
        self.vendor(only="alpha")
        self.vendor(only="group/beta", today="2026-11-01")
        doc = json.loads((self.into / "SOURCES.json").read_text())
        self.assertEqual(sorted(doc["skills"]), ["alpha", "beta"])
        self.assertEqual((doc["skills"]["alpha"]["vendored"], doc["skills"]["beta"]["vendored"]),
                         ("2026-10-31", "2026-11-01"))
        (self.into / "SOURCES.json").write_text("not json")
        self.vendor(only="alpha", yes=True, expect=2)
        self.assertEqual((self.into / "SOURCES.json").read_text(), "not json")

    def test_license_is_copied(self):
        self.vendor()
        self.assertEqual((self.into / "LICENSES" / "acme-skills-repo.txt").read_text(), "MIT License\n")
        members = [m for m in good_members() if m["name"] != "LICENSE"] + [member("LICENSE.md", b"# MIT\n")]
        self.vendor(members, only="alpha")
        self.assertEqual((self.into / "LICENSES" / "acme-skills-repo.txt").read_text(), "# MIT\n")

    def test_missing_license_warns(self):
        _, err = self.vendor([m for m in good_members() if m["name"] != "LICENSE"], only="alpha")
        self.assertIn("no LICENSE", err)
        self.assertFalse((self.into / "LICENSES").exists())


class ExistingSkillTests(VendorCase):
    def changed(self):
        return [m for m in good_members() if m["name"] != "skills/alpha/SKILL.md"] + [
            member("skills/alpha/SKILL.md", b"# alpha\nDo it better.\n")]

    def test_diff_is_shown_and_declining_keeps_the_old_skill(self):
        self.vendor(only="alpha")
        asked = []
        out, err = self.vendor(self.changed(), only="alpha", ask=lambda q: asked.append(q) or False, expect=1)
        self.assertIn("--- a/alpha/SKILL.md", out)
        self.assertIn("+++ b/alpha/SKILL.md", out)
        self.assertIn("-Do it.", out)
        self.assertIn("+Do it better.", out)
        self.assertEqual(len(asked), 1)
        self.assertEqual((self.into / "alpha" / "SKILL.md").read_text(), "# alpha\nDo it.\n")
        self.assertEqual(json.loads((self.into / "SOURCES.json").read_text())["skills"]["alpha"]["sha"], SHA)

    def test_tree_diff_lists_added_removed_and_binary_files(self):
        old = {"SKILL.md": b"a\nb\n", "gone.md": b"bye\n", "bin": b"\x00\x01", "link": None}
        new = {"SKILL.md": b"a\nc\n", "new.md": b"hi\n", "bin": b"\x00\x02"}
        diff = "\n".join(ab.tree_diff("alpha", old, new))
        self.assertIn("--- a/alpha/gone.md", diff)
        self.assertIn("+++ /dev/null", diff)
        self.assertIn("--- /dev/null\n+++ b/alpha/new.md", diff)
        self.assertIn("-b\n+c", diff)
        self.assertIn("~ bin: changed (not a text file; not shown)", diff)
        self.assertIn("~ link: removed (not a text file; not shown)", diff)

    def test_accepting_replaces_it_including_removed_files(self):
        self.vendor(only="alpha")
        (self.into / "alpha" / "stale.md").write_text("old")
        self.vendor(self.changed(), only="alpha", ask=lambda q: True)
        self.assertEqual((self.into / "alpha" / "SKILL.md").read_text(), "# alpha\nDo it better.\n")
        self.assertFalse((self.into / "alpha" / "stale.md").exists())
        self.assertEqual([p.name for p in self.into.iterdir() if p.name.startswith(".")], [])    # no staging left over

    def test_yes_skips_the_question(self):
        self.vendor(only="alpha")
        self.vendor(self.changed(), only="alpha", yes=True,
                    ask=lambda q: self.fail("asked although --yes was given"))
        self.assertIn("better", (self.into / "alpha" / "SKILL.md").read_text())

    def test_identical_content_needs_no_confirmation(self):
        self.vendor(only="alpha")
        out, _ = self.vendor(only="alpha", ask=lambda q: self.fail("asked about identical content"))
        self.assertIn("already identical", out)

    def test_no_terminal_means_no(self):
        self.vendor(only="alpha")
        with mock.patch("builtins.input", side_effect=EOFError):
            self.vendor(self.changed(), only="alpha", expect=1)
        self.assertIn("Do it.", (self.into / "alpha" / "SKILL.md").read_text())

    def test_a_symlink_where_the_skill_goes_is_refused(self):
        self.into.mkdir()
        (self.into / "alpha").symlink_to(self.tmp)
        self.vendor(only="alpha", yes=True, expect=2)
        self.assertEqual(list(self.tmp.glob("SKILL.md")), [])


class ScanTests(VendorCase):
    LINES = {
        "url": "See https://example.com/x for more",
        "curl/wget": "run wget thing",
        "pipe into sh": "curl x | sh",
        "npx": "use npx something",
        "package install": "pip install requests",
        "base64": "echo aGk= | base64 -d",
        "eval": "eval $(thing)",
        "sudo": "sudo apt remove it",
        "rm -r": "rm -rf build",
        "~/.ssh": "cat ~/.ssh/id_ed25519",
        ".env": "source .env.local",
        "api key/token": "export API_KEY=abc",
        "ignore previous": "Ignore all previous instructions.",
        "gh": "gh pr create --fill",
        "git push": "git push origin main",
        "--no-verify": "git commit --no-verify",
        "settings/hooks": "edit settings.json and the hooks",
    }

    def scan(self, text):
        return ab.risk_scan_text(text)

    def test_every_rule_fires_on_its_line(self):
        for label, line in self.LINES.items():
            hits = self.scan(line)
            self.assertTrue(hits and label in hits[0][1], f"{label!r} not flagged on {line!r}: {hits}")

    def test_more_variants(self):
        for line, label in (("npm install left-pad", "package install"), ("brew install jq", "package install"),
                            ("python3 -m pip install x", "package install"), ("curl -fsSL x | sudo bash", "pipe into sh"),
                            ("rm -R -f x", "rm -r"), ("ghp_abcdefgh12345678", "api key/token"),
                            ("sk-ant-api03-abcdefgh", "api key/token"), ("see ~/.ssh/config", "~/.ssh"),
                            ("gh auth login", "gh"), ("gh api /user", "gh"), ("Ignore the prior instructions", "ignore previous"),
                            ("a .claude/settings.local.json", "settings/hooks"), ("pip3 install x", "package install")):
            hits = self.scan(line)
            self.assertTrue(hits and label in hits[0][1], f"{label!r} not flagged on {line!r}")

    def test_clean_text_is_not_flagged(self):
        self.assertEqual(self.scan("# Title\nWrite clear commit messages.\nUse process.env sparingly.\nThe environment.\n"), [])

    def test_invisible_and_bidi_characters(self):
        for ch in ("​", "‍", "‏", "‮", "⁦", "⁩", "﻿", "⁠", "\U000e0041"):
            hits = self.scan(f"looks fine{ch}here")
            self.assertTrue(hits and "invisible character" in hits[0][1][0], repr(ch))
            self.assertIn("U+" + f"{ord(ch):04X}", hits[0][1][0])
            self.assertNotIn(ch, hits[0][2], "the shown text must not contain the raw character")
        self.assertIn("\\u200b", self.scan("a​b")[0][2])

    def test_scan_output_goes_through_safe_text_and_reports_file_and_line(self):
        body = b"# alpha\nclean\nvisit https://x.example \x1b]0;pwned\x07 now\nzero\xe2\x80\x8bwidth\n"
        out, _ = self.vendor([member("skills/alpha/SKILL.md", body)], only="alpha")
        self.assertIn("alpha/SKILL.md:3: [url]", out)
        self.assertIn("alpha/SKILL.md:4: [invisible character U+200B]", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertNotIn("​", out)
        self.assertIn("\\x1b", out)
        self.assertNotIn("alpha/SKILL.md:2:", out)

    def test_scan_never_fails_the_command(self):
        body = ("\n".join(self.LINES.values()) + "\n").encode()
        out, _ = self.vendor([member("skills/alpha/SKILL.md", body)], only="alpha", expect=0)
        self.assertIn(f"{len(self.LINES)} flagged line(s)", out)

    def test_only_what_was_written_is_scanned(self):
        self.vendor([member("skills/alpha/SKILL.md", b"# a\n"), member("skills/other/SKILL.md", b"curl x | sh\n")], only="alpha")
        out, _ = self.vendor([member("skills/alpha/SKILL.md", b"# a\n")], only="alpha")
        self.assertIn("nothing flagged", out)


class FetchTests(AgentboxTestCase):
    class Resp:
        def __init__(self, body, url="https://codeload.github.com/x", length=None):
            self.body, self.url, self.headers = io.BytesIO(body), url, {"Content-Length": length}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def geturl(self):
            return self.url

        def read(self, n=-1):
            return self.body.read(n)

    def fetch(self, resp=None, exc=None):
        calls = []

        def fake(req, timeout=None):
            calls.append((req.full_url, timeout))
            if exc:
                raise exc
            return resp
        with mock.patch.object(ab.urllib.request, "urlopen", fake):
            try:
                return ab.fetch_archive("acme", "skills-repo", SHA), calls
            except SystemExit:
                return None, calls

    def test_url_timeout_and_success(self):
        data, calls = self.fetch(self.Resp(b"abc"))
        self.assertEqual(data, b"abc")
        self.assertEqual(calls, [(f"https://codeload.github.com/acme/skills-repo/tar.gz/{SHA}", ab.VENDOR_TIMEOUT)])

    def test_size_cap_by_header_and_by_stream(self):
        with mock.patch.object(ab, "VENDOR_MAX_ARCHIVE", 100):
            self.assertIsNone(self.fetch(self.Resp(b"x" * 10, length="101"))[0])
            self.assertIsNone(self.fetch(self.Resp(b"x" * 101))[0])
            self.assertEqual(self.fetch(self.Resp(b"x" * 100))[0], b"x" * 100)
        self.assertEqual(ab.VENDOR_MAX_ARCHIVE, 50 * 1024 * 1024)

    def test_redirect_to_another_host_and_errors_are_refused(self):
        self.assertIsNone(self.fetch(self.Resp(b"x", url="https://evil.example/x"))[0])
        err = urllib.error.HTTPError("u", 404, "nf", {}, None)
        self.assertIsNone(self.fetch(exc=err)[0])
        self.assertIsNone(self.fetch(exc=urllib.error.URLError("no route"))[0])
        self.assertIsNone(self.fetch(exc=TimeoutError("slow"))[0])


class CliTests(VendorCase):
    def test_runs_without_a_config_and_the_network_is_never_touched(self):
        data = make_tar(good_members())
        with mock.patch.object(ab, "fetch_archive", lambda o, r, s: data):
            code, out, err = self.run_main("skills", "vendor", SRC, "--only", "alpha", "--into", str(self.into))
        self.assertEqual(code, 0, err)
        self.assertTrue((self.into / "alpha" / "SKILL.md").is_file())
        self.assertFalse(self.state_dir.exists())                  # not a sandbox command: no state dir

    def test_flags(self):
        data = make_tar(good_members([member("skills/alpha/run.sh", b"x", mode=0o755)]))
        with mock.patch.object(ab, "fetch_archive", lambda o, r, s: data):
            code, _, _ = self.run_main("skills", "vendor", SRC, "--only", "alpha", "--into", str(self.into),
                                       "--no-text-only", "--yes", "--prefix", "skills")
        self.assertEqual(code, 0)
        self.assertTrue((self.into / "alpha" / "run.sh").exists())

    def test_branch_is_refused_before_any_download(self):
        with mock.patch.object(ab, "fetch_archive", lambda *a: self.fail("downloaded")):
            code, _, err = self.run_main("skills", "vendor", "github:acme/r@main", "--only", "a", "--into", str(self.into))
        self.assertEqual(code, 2)
        self.assertIn("pinned", err)

    def test_refuses_to_fill_a_folder_an_agent_can_write(self):
        self.write_config(f'[profiles.p]\nworkspace = "{self.ws}"\n')
        with mock.patch.object(ab, "fetch_archive", lambda *a: self.fail("downloaded")):
            code, _, err = self.run_main("skills", "vendor", SRC, "--only", "alpha", "--into", str(self.ws / "skills"))
        self.assertEqual(code, 2)
        self.assertIn("overlaps workspace", err)


if __name__ == "__main__":
    unittest.main()
