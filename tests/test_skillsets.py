import argparse
import contextlib
import io
import json
import os
import subprocess
import unittest
from unittest import mock

from helpers import AgentboxTestCase, FakeSbx, LocalVM, ab

HEX = "0123456789abcdef0123456789abcdef01234567"


class SkillCase(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.sets = self.tmp / "skills"
        self.sets.mkdir()
        # the home dir the "~/.claude" rule looks at must not be a parent of the temp dirs
        self.home = self.tmp / "home"
        self.home.mkdir()
        p = mock.patch.object(ab, "HOME", self.home)
        p.start()
        self.addCleanup(p.stop)

    def make_set(self, name="core", skills=("alpha", "beta"), where=None):
        root = where or (self.sets / name)
        for sk in skills:
            (root / sk).mkdir(parents=True, exist_ok=True)
            (root / sk / "SKILL.md").write_text(f"# {sk}\nDo the thing.\n")
        return root

    def config(self, sets=None, profile_extra="", defaults="", ws=None):
        """sets: {name: toml body}. Defaults to one set 'core' made on disk."""
        if sets is None:
            sets = {"core": f'path = "{self.make_set()}"'}
        body = "".join(f"[skillsets.{n}]\n{b}\n" for n, b in sets.items())
        return self.write_config(f'''
            {defaults}
            {body}
            [profiles.p]
            workspace = "{ws or self.ws}"
            {profile_extra}
        ''')

    def inspect(self, name="core", **kw):
        return ab.inspect_skillset(self.config(**kw), name)


class ConfigTests(SkillCase):
    def test_skills_merge_with_defaults_and_unknown_set_is_refused(self):
        a, b = self.make_set("a", ("one",)), self.make_set("b", ("two",))
        cfg = self.config({"a": f'path = "{a}"', "b": f'path = "{b}"'}, 'skills = ["b", "a"]',
                          defaults='[defaults]\nskills = ["a"]')
        self.assertEqual(cfg.profile("p").d["skills"], ["a", "b"])
        cfg = self.config(profile_extra='skills = ["nope"]')
        with self.assertRaises(SystemExit):
            cfg.profile("p")

    def test_no_skills_by_default(self):
        self.assertEqual(self.config().profile("p").d["skills"], [])
        self.assertEqual(self.config().profile("p").skill_mounts(), [])


class RuleTests(SkillCase):
    def problems(self, **kw):
        return " | ".join(self.inspect(**kw).problems)

    def test_clean_set(self):
        self.make_set()
        (self.sets / "core" / "alpha" / "ref").mkdir()
        (self.sets / "core" / "alpha" / "ref" / "notes.txt").write_text("x")
        (self.sets / "core" / "alpha" / "LICENSE").write_text("MIT")
        (self.sets / "core" / "LICENSE").write_text("MIT")       # top-level files are never looked at
        (self.sets / "core" / "package.json").write_text("{}")
        (self.sets / "core" / "run.sh").write_text("echo")
        ss = self.inspect()
        self.assertEqual((ss.problems, ss.skills, ss.commit), ([], ["alpha", "beta"], None))
        self.assertEqual(ss.path, (self.sets / "core").resolve())

    def test_path_rules(self):
        self.assertIn("needs `path", self.problems(sets={"core": 'description = "x"'}))
        self.assertIn("does not exist", self.problems(sets={"core": f'path = "{self.tmp}/missing"'}))
        f = self.tmp / "afile"
        f.write_text("x")
        self.assertIn("not a directory", self.problems(sets={"core": f'path = "{f}"'}))
        real = self.make_set("real")
        link = self.tmp / "link"
        link.symlink_to(real)
        self.assertIn("is a symlink", self.problems(sets={"core": f'path = "{link}"'}))
        self.assertIn("must be absolute", self.problems(sets={"core": 'path = "relative/dir"'}))
        self.assertIn("allow_scripts must be", self.problems(sets={"core": f'path = "{real}"\nallow_scripts = "yes"'}))

    def test_set_inside_or_around_a_writable_workspace_of_any_profile(self):
        inside = self.make_set(where=self.ws / "skills")
        self.assertIn("overlaps workspace", self.problems(sets={"core": f'path = "{inside}"'}))
        other = self.tmp / "other-ws"
        other.mkdir()
        around = self.make_set(where=self.tmp / "around")
        cfg = self.write_config(f'''
            [skillsets.core]
            path = "{around}"
            [profiles.p]
            workspace = "{self.ws}"
            [profiles.q]
            enabled = false
            workspace = "{other}"
            extra_workspaces = ["{around / 'alpha'}"]
        ''')
        self.assertIn("overlaps workspace", " ".join(ab.inspect_skillset(cfg, "core").problems))
        self.assertEqual(ab.inspect_skillset(self.config(), "core").problems, [])

    def overlap(self, ws_toml, set_path=None):
        set_path = set_path or self.make_set(where=self.tmp / "repo")
        cfg = self.write_config(f'''
            [skillsets.core]
            path = "{set_path}"
            [profiles.p]
            workspace = "{self.ws}"
            {ws_toml}
        ''')
        return " ".join(ab.inspect_skillset(cfg, "core").problems)

    def test_clone_mode_workspace_and_read_only_extras_do_not_count(self):
        repo = self.make_set(where=self.tmp / "repo")
        self.assertEqual(self.overlap('', repo), "")
        cfg = self.write_config(f'''
            [skillsets.core]
            path = "{repo}"
            [profiles.cl]
            workspace = "{repo}"
            mode = "clone"
            [profiles.ro]
            workspace = "{self.ws}"
            extra_workspaces = ["{repo}:ro", "{repo / 'alpha'}:ro"]
        ''')
        self.assertEqual(ab.inspect_skillset(cfg, "core").problems, [])
        cfg = self.write_config(f'''
            [defaults]
            mode = "clone"
            [skillsets.core]
            path = "{repo}"
            [profiles.cl]
            workspace = "{repo}"
        ''')
        self.assertEqual(ab.inspect_skillset(cfg, "core").problems, [])         # mode inherited from [defaults]

    def test_direct_mode_workspace_and_writable_extras_are_refused(self):
        repo = self.make_set(where=self.tmp / "repo")
        for body in (f'workspace = "{repo}"', f'workspace = "{repo}"\nmode = "direct"'):
            cfg = self.write_config(f'[skillsets.core]\npath = "{repo}"\n[profiles.d]\n{body}\n')
            self.assertIn("overlaps workspace", " ".join(ab.inspect_skillset(cfg, "core").problems))
        for extra in (f'"{repo}"', f'"{repo}:rw"'):
            for mode in ("direct", "clone"):          # an extra is writable in either mode
                self.assertIn("overlaps workspace", self.overlap(f'mode = "{mode}"\nextra_workspaces = [{extra}]', repo))

    def test_config_kits_state_and_claude_dir(self):
        cases = {
            "inside the config dir": self.make_set(where=self.cfg_dir / "skills"),
            "contains the config file": self.make_set(where=self.tmp),         # tmp holds cfg/, state/, ws/
            "overlaps": self.make_set(where=self.home / ".claude" / "skills"),
        }
        for want, path in cases.items():
            self.assertIn(want, self.problems(sets={"core": f'path = "{path}"'}), want)
        self.assertIn("contains the state dir", self.problems(sets={"core": f'path = "{self.tmp}"'}))
        self.assertIn("overlaps", self.problems(sets={"core": f'path = "{self.home}"'}))

    def test_skill_folders(self):
        root = self.make_set("core", ("alpha",))
        (root / "Bad_Name").mkdir()
        (root / "Bad_Name" / "SKILL.md").write_text("x")
        (root / "simplify").mkdir()
        (root / "simplify" / "SKILL.md").write_text("x")
        (root / "empty").mkdir()
        (root / "-dash").mkdir()
        (root / "-dash" / "SKILL.md").write_text("x")
        (root / "long-" .ljust(70, "x")).mkdir()
        got = self.problems(sets={"core": f'path = "{root}"'})
        self.assertIn("'Bad_Name'", got)
        self.assertIn("'simplify' would shadow a built-in", got)
        self.assertIn("'empty' has no SKILL.md", got)
        self.assertIn("'-dash'", got)
        self.assertIn("long-", got)
        for builtin in ab.BUILTIN_SKILLS:
            self.assertRegex(builtin, ab.SKILL_NAME_RE)
        self.assertEqual(len(ab.BUILTIN_SKILLS), 12)

    def test_skill_md_must_be_a_real_file(self):
        root = self.make_set("core", ("alpha",))
        (root / "alpha" / "SKILL.md").unlink()
        (root / "alpha" / "SKILL.md").mkdir()
        self.assertIn("no SKILL.md", self.problems(sets={"core": f'path = "{root}"'}))

    def test_symlinks_anywhere_are_refused_even_with_allow_scripts(self):
        root = self.make_set("core", ("alpha",))
        (root / "alpha" / "deep").mkdir()
        (root / "alpha" / "deep" / "ln").symlink_to("/etc/passwd")
        (root / "alpha" / "dirlink").symlink_to(self.tmp)
        (root / "toplink").symlink_to(self.tmp)
        (root / ".hidden-link").symlink_to(self.tmp)
        for extra in ("", "\nallow_scripts = true"):
            got = self.problems(sets={"core": f'path = "{root}"{extra}'})
            for name in ("alpha/deep/ln", "alpha/dirlink", "toplink", ".hidden-link"):
                self.assertIn(name, got)

    def test_text_only_and_executable_bit(self):
        root = self.make_set("core", ("alpha",))
        a = root / "alpha"
        (a / "run.sh").write_text("echo hi")
        (a / "tool.py").write_text("print(1)")
        (a / "img.png").write_bytes(b"\x89PNG")
        (a / "x.md").write_text("ok")
        (a / "x.md").chmod(0o755)
        (a / "good.YAML").write_text("a: 1")
        (a / "LICENSE-MIT").write_text("MIT")
        got = self.problems(sets={"core": f'path = "{root}"'})
        for bad in ("alpha/run.sh", "alpha/tool.py", "alpha/img.png", "alpha/x.md"):
            self.assertIn(bad, got)
        for good in ("good.YAML", "LICENSE-MIT", "alpha/SKILL.md"):
            self.assertNotIn(good, got)
        self.assertEqual(self.problems(sets={"core": f'path = "{root}"\nallow_scripts = true'}), "")

    def test_fifo_is_refused(self):
        root = self.make_set("core", ("alpha",))
        os.mkfifo(root / "alpha" / "pipe.md")
        for extra in ("", "\nallow_scripts = true"):
            self.assertIn("alpha/pipe.md", self.problems(sets={"core": f'path = "{root}"{extra}'}))

    def test_name_collisions_across_sets_in_one_profile(self):
        a, b = self.make_set("a", ("shared", "one")), self.make_set("b", ("shared", "two"))
        cfg = self.config({"a": f'path = "{a}"', "b": f'path = "{b}"'}, 'skills = ["a", "b"]')
        got = cfg.profile("p").skill_problems()
        self.assertEqual(len(got), 1)
        self.assertIn("'shared' is in both set 'a' and set 'b'", got[0])
        with self.assertRaises(SystemExit):
            cfg.profile("p").require_skills()
        cfg = self.config({"a": f'path = "{a}"', "b": f'path = "{b}"'}, 'skills = ["a"]')
        self.assertEqual(cfg.profile("p").skill_problems(), [])

    def test_require_skills_names_the_problem(self):
        bad = self.make_set(where=self.ws / "skills")
        cfg = self.config({"core": f'path = "{bad}"'}, 'skills = ["core"]')
        code, _, err = self.run_main("up", "p")
        self.assertEqual(code, 2)
        self.assertIn("skill sets refused", err)
        self.assertIn("overlaps workspace", err)


class CommitTests(SkillCase):
    def repo(self, head, refs=None, packed=None):
        root = self.make_set()
        g = root / ".git"
        (g / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (g / "packed-refs").unlink(missing_ok=True)
        (g / "HEAD").write_text(head)
        for ref, sha in (refs or {}).items():
            (g / ref).write_text(sha + "\n")
        if packed:
            (g / "packed-refs").write_text("# pack-refs\n" + packed)
        return root

    def test_not_a_repo(self):
        self.assertIsNone(self.inspect().commit)

    def test_branch_detached_packed_and_unreadable(self):
        root = self.repo("ref: refs/heads/main\n", {"refs/heads/main": HEX})
        self.assertEqual(ab.git_commit_of(root), HEX)
        self.assertEqual(self.inspect(sets={"core": f'path = "{root}"'}).commit, HEX)
        root = self.repo("ref: refs/heads/main\n", packed=f"{HEX} refs/heads/main\n")
        self.assertEqual(ab.git_commit_of(root), HEX)
        root = self.repo(HEX + "\n")
        self.assertEqual(ab.git_commit_of(root), HEX)
        root = self.repo("ref: refs/heads/gone\n")
        self.assertEqual(ab.git_commit_of(root), "unknown")
        root = self.repo("ref: refs/../../etc/passwd\n")
        self.assertEqual(ab.git_commit_of(root), "unknown")

    def test_git_dir_is_not_scanned_for_skills_or_text_rules(self):
        root = self.repo("ref: refs/heads/main\n", {"refs/heads/main": HEX})
        hook = root / ".git" / "hooks"
        hook.mkdir()
        (hook / "pre-commit").write_text("#!/bin/sh")
        (hook / "pre-commit").chmod(0o755)
        self.assertEqual(self.inspect(sets={"core": f'path = "{root}"'}).problems, [])

    def test_never_runs_git(self):
        # subprocess.run is a loud failure in these tests; reading must not need it
        self.repo("ref: refs/heads/main\n", {"refs/heads/main": HEX})
        self.assertIsNotNone(ab.git_commit_of(self.sets / "core"))


class RepoCase(SkillCase):
    """A repo-shaped set: skills/<bucket>/<skill>/SKILL.md plus things that are not skills."""

    def make_repo(self, where=None):
        root = where or (self.tmp / "repo")
        for rel in ("skills/engineering/tdd", "skills/productivity/grilling", "skills/local/sandbox",
                    "skills/engineering/diagnosing-bugs"):
            (root / rel).mkdir(parents=True, exist_ok=True)
            (root / rel / "SKILL.md").write_text("# s\n")
        (root / "skills" / "engineering" / "diagnosing-bugs" / "scripts").mkdir(exist_ok=True)
        (root / "skills" / "engineering" / "diagnosing-bugs" / "scripts" / "run.sh").write_text("echo")
        (root / "scripts").mkdir(exist_ok=True)
        (root / "scripts" / "link.sh").write_text("#!/bin/sh")
        (root / "scripts" / "link.sh").chmod(0o755)
        (root / "CLAUDE.md").write_text("x")
        if not (root / "AGENTS.md").is_symlink():
            (root / "AGENTS.md").symlink_to("CLAUDE.md")
        (root / "package.json").write_text("{}")
        (root / ".github").mkdir(exist_ok=True)
        (root / ".github" / "ci.yml").write_text("x")
        (root / ".claude-plugin").mkdir(exist_ok=True)
        (root / ".claude-plugin" / "plugin.json").write_bytes(b"\x00")
        return root

    INC = '["skills/engineering/tdd", "skills/productivity/grilling", "skills/local/sandbox"]'

    def repo_set(self, extra="", include=None):
        root = self.make_repo()
        inc = self.INC if include is None else include
        return ab.inspect_skillset(self.config({"core": f'path = "{root}"\ninclude = {inc}\n{extra}'}), "core")


class IncludeTests(RepoCase):
    def test_only_included_folders_are_skills_and_the_rest_is_not_checked(self):
        ss = self.repo_set()
        self.assertEqual(ss.problems, [])          # symlink, script, binary and .github are outside the include
        self.assertEqual(ss.skills, ["tdd", "grilling", "sandbox"])
        self.assertEqual(ss.folders["tdd"], "skills/engineering/tdd")
        self.assertEqual(ss.include, ["skills/engineering/tdd", "skills/productivity/grilling", "skills/local/sandbox"])

    def test_without_include_the_repo_root_is_flat_and_fails_on_its_layout(self):
        root = self.make_repo()
        got = " ".join(ab.inspect_skillset(self.config({"core": f'path = "{root}"'}), "core").problems)
        self.assertIn("'skills' has no SKILL.md", got)
        self.assertIn("symlinks are not allowed: AGENTS.md", got)

    def test_included_folders_get_every_rule(self):
        root = self.make_repo()
        tdd = root / "skills" / "engineering" / "tdd"
        (tdd / "ln").symlink_to("/etc")
        (tdd / "tool.py").write_text("print(1)")
        os.mkfifo(tdd / "pipe.md")
        (root / "skills" / "local" / "sandbox" / "SKILL.md").unlink()
        got = " ".join(ab.inspect_skillset(self.config({"core": f'path = "{root}"\ninclude = {self.INC}'}), "core").problems)
        for want in ("skills/engineering/tdd/ln", "skills/engineering/tdd/tool.py", "skills/engineering/tdd/pipe.md",
                     "'sandbox' has no SKILL.md"):
            self.assertIn(want, got)

    def test_name_rule_and_builtin_clash_use_the_last_component(self):
        root = self.make_repo()
        for rel in ("skills/x/Bad_Name", "skills/x/simplify"):
            (root / rel).mkdir(parents=True)
            (root / rel / "SKILL.md").write_text("x")
        got = " ".join(ab.inspect_skillset(
            self.config({"core": f'path = "{root}"\ninclude = ["skills/x/Bad_Name", "skills/x/simplify"]'}), "core").problems)
        self.assertIn("a skill name must match", got)
        self.assertIn("'simplify' would shadow a built-in", got)

    def test_two_paths_with_the_same_last_component_are_an_error(self):
        root = self.make_repo()
        (root / "skills" / "local" / "tdd").mkdir()
        (root / "skills" / "local" / "tdd" / "SKILL.md").write_text("x")
        got = " ".join(ab.inspect_skillset(self.config(
            {"core": f'path = "{root}"\ninclude = ["skills/engineering/tdd", "skills/local/tdd"]'}), "core").problems)
        self.assertIn("two folders named 'tdd'", got)

    def test_bad_include_paths(self):
        root = self.make_repo()
        (root / "linked").symlink_to(root / "skills")
        (root / "skills" / "engineering" / "deep").symlink_to(root / "skills" / "local")
        (root / "afile").write_text("x")
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("x")
        cases = {
            str(outside): "must be relative",
            "/skills/engineering/tdd": "must be relative",
            "../outside": "must not contain",
            "skills/../skills/engineering/tdd": "must not contain",
            "linked/engineering/tdd": "goes through a symlink",
            "skills/engineering/deep/sandbox": "goes through a symlink",
            "skills/engineering/deep": "goes through a symlink",
            "skills/nope": "does not exist",
            "afile": "is not a directory",
            ".": "names the set itself",
            "": "non-empty",
        }
        for inc, want in cases.items():
            got = " ".join(ab.inspect_skillset(self.config(
                {"core": f'path = "{root}"\ninclude = [{json.dumps(inc)}]'}), "core").problems)
            self.assertIn(want, got, inc)
        for bad in ('"skills/engineering/tdd"', "[]", "[1]"):
            got = " ".join(ab.inspect_skillset(self.config({"core": f'path = "{root}"\ninclude = {bad}'}), "core").problems)
            self.assertIn("include must be", got, bad)

    def test_trailing_slash_and_dot_components_are_normalised(self):
        ss = self.repo_set(include='["skills/./engineering/tdd/"]')
        self.assertEqual((ss.problems, ss.include), ([], ["skills/engineering/tdd"]))

    def test_links_point_at_the_folder_inside_the_mount(self):
        root = self.make_repo()
        cfg = self.config({"core": f'path = "{root}"\ninclude = {self.INC}'}, 'skills = ["core"]')
        links = dict(cfg.profile("p").skill_links())
        self.assertEqual(links["tdd"], f"{root}/skills/engineering/tdd")
        self.assertEqual(links["sandbox"], f"{root}/skills/local/sandbox")


class AllowScriptsTests(RepoCase):
    DB = "skills/engineering/diagnosing-bugs"

    def inc(self):
        return f'["skills/engineering/tdd", "{self.DB}"]'

    def test_false_by_default_true_and_list(self):
        self.assertIn(f"{self.DB}/scripts/run.sh", " ".join(self.repo_set(include=self.inc()).problems))
        self.assertEqual(self.repo_set("allow_scripts = true", self.inc()).problems, [])
        ss = self.repo_set(f'allow_scripts = ["{self.DB}"]', self.inc())
        self.assertEqual(ss.problems, [])

    def test_list_covers_only_the_listed_paths(self):
        root = self.make_repo()
        (root / "skills" / "engineering" / "tdd" / "x.py").write_text("1")
        cfg = self.config({"core": f'path = "{root}"\ninclude = {self.inc()}\nallow_scripts = ["{self.DB}"]'})
        got = " ".join(ab.inspect_skillset(cfg, "core").problems)
        self.assertIn("tdd/x.py", got)
        self.assertNotIn("run.sh", got)

    def test_a_path_that_is_not_included_is_an_error(self):
        got = " ".join(self.repo_set('allow_scripts = ["skills/local/sandbox"]', self.inc()).problems)
        self.assertIn("'skills/local/sandbox', which is not in include", got)
        flat = self.make_set()
        got = " ".join(ab.inspect_skillset(self.config({"core": f'path = "{flat}"\nallow_scripts = ["alpha"]'}), "core").problems)
        self.assertIn("no `include`", got)

    def test_symlinks_and_fifos_stay_refused_even_when_covered(self):
        root = self.make_repo()
        tdd = root / "skills" / "engineering" / "tdd"
        (tdd / "ln").symlink_to("/etc")
        os.mkfifo(tdd / "pipe")
        cfg = self.config({"core": f'path = "{root}"\ninclude = ["skills/engineering/tdd"]\n'
                                   'allow_scripts = ["skills/engineering/tdd"]'})
        got = " ".join(ab.inspect_skillset(cfg, "core").problems)
        self.assertIn("tdd/ln", got)
        self.assertIn("tdd/pipe", got)


class RefTests(RepoCase):
    OTHER = "f" * 40

    def git_repo(self, head=HEX, branch="main"):
        root = self.make_repo()
        g = root / ".git"
        (g / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (g / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (g / "refs" / "heads" / branch).write_text(head + "\n")
        return root

    def inspect_repo(self, root, ref=None):
        extra = f'\nref = "{ref}"' if ref is not None else ""
        return ab.inspect_skillset(self.config({"core": f'path = "{root}"\ninclude = {self.INC}{extra}'}), "core")

    def test_head_and_branch_are_read_without_git(self):
        ss = self.inspect_repo(self.git_repo())
        self.assertEqual((ss.commit, ss.branch, ss.ref, ss.problems), (HEX, "main", None, []))
        (self.tmp / "repo" / ".git" / "HEAD").write_text(HEX + "\n")
        self.assertEqual(self.inspect_repo(self.tmp / "repo").branch, None)

    def test_matching_ref_passes_and_is_case_insensitive(self):
        root = self.git_repo()
        self.assertEqual(self.inspect_repo(root, HEX).problems, [])
        self.assertEqual(self.inspect_repo(root, HEX.upper()).ref, HEX)

    def test_mismatch_prints_both_commits(self):
        got = " ".join(self.inspect_repo(self.git_repo(), self.OTHER).problems)
        self.assertIn(f"HEAD is {HEX}", got)
        self.assertIn(f"ref pins {self.OTHER}", got)

    def test_ref_on_a_folder_that_is_not_a_work_tree_is_refused(self):
        self.assertIn("HEAD is not a git work tree", " ".join(self.inspect_repo(self.make_repo(), HEX).problems))

    def test_ref_must_be_40_hex(self):
        root = self.git_repo()
        for bad in ("main", HEX[:12], HEX + "0", "g" * 40):
            self.assertIn("ref must be a full commit", " ".join(self.inspect_repo(root, bad).problems), bad)
        cfg = self.config({"core": f'path = "{root}"\ninclude = {self.INC}\nref = 5'})
        self.assertIn("ref must be a full commit", " ".join(ab.inspect_skillset(cfg, "core").problems))

    def test_create_and_link_refuse_on_mismatch(self):
        root = self.git_repo()
        cfg = self.config({"core": f'path = "{root}"\ninclude = {self.INC}\nref = "{self.OTHER}"'}, 'skills = ["core"]')
        rec = Recorder()
        with mock.patch.object(ab, "sbx", rec), \
                mock.patch.object(ab, "sandbox_exists", lambda prof, retry=False: False):
            with self.assertRaises(SystemExit):
                ab.ensure_sandbox(cfg.profile("p"))
            with self.assertRaises(SystemExit):
                ab.sync_skills(cfg.profile("p"), background=False)
        self.assertEqual(rec.calls, [])
        code, _, err = self.run_main("up", "p")
        self.assertEqual(code, 2)
        self.assertIn(self.OTHER, err)
        self.assertIn(HEX, err)


class DriftTests(RepoCase):
    NEW = "e" * 40

    def setUp(self):
        super().setUp()
        self.root = self.tmp / "repo"
        self.make_repo(self.root)
        self.move(HEX, "main")

    def move(self, commit, branch):
        g = self.root / ".git"
        (g / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (g / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
        (g / "refs" / "heads" / branch).write_text(commit + "\n")

    def prof(self, extra=""):
        return self.config({"core": f'path = "{self.root}"\ninclude = {RepoCase.INC}\n{extra}'},
                           'skills = ["core"]').profile("p")

    def create(self, extra=""):
        prof = self.prof(extra)
        with mock.patch.object(ab, "sbx", Recorder()), \
                mock.patch.object(ab, "sandbox_exists", lambda prof, retry=False: False):
            ab.ensure_sandbox(prof)
        return prof

    def existing(self, extra=""):
        prof = self.prof(extra)
        err = io.StringIO()
        with mock.patch.object(ab, "sbx", Recorder()), mock.patch.object(ab, "start_bridge", lambda p: None), \
                mock.patch.object(ab, "sandbox_exists", lambda prof, retry=False: True), contextlib.redirect_stderr(err):
            ab.ensure_sandbox(prof)
        return err.getvalue()

    def test_creation_records_head_and_branch(self):
        prof = self.create()
        self.assertEqual(json.loads((prof.state / "skills.heads").read_text()),
                         {"core": {"commit": HEX, "branch": "main"}})

    def test_no_change_no_warning(self):
        self.create()
        self.assertNotIn("WARNING", self.existing())

    def test_new_commit_warns_with_both_commits_and_the_diff_hint(self):
        self.create()
        self.move(self.NEW, "main")
        err = self.existing()
        self.assertIn("WARNING", err)
        self.assertIn("running sandboxes already see the new content", err)
        self.assertIn(f"git -C {self.root} diff {HEX} {self.NEW} -- skills/engineering/tdd skills/productivity/grilling"
                      " skills/local/sandbox", err)

    def test_other_branch_warns_even_at_the_same_commit(self):
        self.create()
        self.move(HEX, "review")
        err = self.existing()
        self.assertIn("checked-out branch", err)
        self.assertIn("was main", err)
        self.assertIn("now review", err)

    def test_pinned_set_never_warns_and_is_refused_instead(self):
        self.create(f'ref = "{HEX}"')
        self.assertNotIn("WARNING", self.existing(f'ref = "{HEX}"'))
        self.move(self.NEW, "main")
        with self.assertRaises(SystemExit):
            self.existing(f'ref = "{HEX}"')

    def test_sandbox_made_before_recording_does_not_warn(self):
        prof = self.create()
        (prof.state / "skills.heads").unlink()
        self.move(self.NEW, "main")
        self.assertNotIn("WARNING", self.existing())

    def test_status_and_doctor_show_the_set_and_the_drift(self):
        self.create()
        self.move(self.NEW, "review")
        prof = self.prof()
        row = ab.skills_status(prof)[0]
        self.assertEqual((row["ref"], row["commit"], row["branch"], row["drift"], row["skills"]),
                         (None, self.NEW, "review", "changed", ["tdd", "grilling", "sandbox"]))
        self.assertEqual(ab.skill_drift(self.prof(f'ref = "{self.NEW}"'), ab.inspect_skillset(
            self.config({"core": f'path = "{self.root}"\ninclude = {RepoCase.INC}\nref = "{self.NEW}"'}), "core"))[0], "pinned")


class KitTests(SkillCase):
    def test_instructions_name_the_sets_and_say_read_only(self):
        prof = self.config(profile_extra='skills = ["core"]').profile("p")
        ab.render_local_kit(prof, "tok")
        spec = (prof.state / "kit" / "spec.yaml").read_text()
        self.assertIn(str((self.sets / "core").resolve()), spec)
        self.assertIn("read-only", spec)

    def test_no_skills_no_instructions_and_digest_is_stable(self):
        plain = self.config().profile("p")
        _, d_plain = ab.render_local_kit(plain, "tok")
        self.assertNotIn("Skill sets", (plain.state / "kit" / "spec.yaml").read_text())
        # a profile that never used skills hashes exactly what it did before the key existed
        text = (plain.state / "kit" / "spec.yaml").read_text()
        legacy = {k: v for k, v in plain.d.items() if k != "skills"}
        self.assertEqual(d_plain, ab.hashlib.sha256(
            (text + json.dumps({}, sort_keys=True) + json.dumps(legacy, sort_keys=True, default=str)).encode()).hexdigest())

    def test_digest_changes_with_skills_and_with_the_set_definition(self):
        base = ab.render_local_kit(self.config().profile("p"), "t")[1]
        with_skills = ab.render_local_kit(self.config(profile_extra='skills = ["core"]').profile("p"), "t")[1]
        self.assertNotEqual(base, with_skills)
        other = self.make_set("elsewhere")
        moved = ab.render_local_kit(
            self.config({"core": f'path = "{other}"'}, 'skills = ["core"]').profile("p"), "t")[1]
        self.assertNotEqual(with_skills, moved)
        # ref / include / allow_scripts apply to the live mount: no `fresh` needed
        live = ab.render_local_kit(
            self.config({"core": f'path = "{self.sets / "core"}"\nallow_scripts = true\nref = "{HEX}"\n'
                                 'include = ["alpha"]'}, 'skills = ["core"]').profile("p"), "t")[1]
        self.assertEqual(with_skills, live)


class Recorder:
    """Stand-in for sbx during ensure_sandbox: records `create`, answers nothing else."""

    def __init__(self):
        self.calls = []

    def __call__(self, cfg, *args, check=True, capture=False, stdin=None):
        self.calls.append(args)
        if args[0] in ("create", "ports"):
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(f"unexpected sbx call {args!r}")


class MountTests(SkillCase):
    def ensure(self, exists, **cfg_kw):
        prof = self.config(**cfg_kw).profile("p")
        rec = Recorder()
        with mock.patch.object(ab, "sbx", rec), \
                mock.patch.object(ab, "sandbox_exists", lambda prof, retry=False: exists):
            ab.ensure_sandbox(prof)
        return prof, rec

    def test_create_mounts_each_set_read_only_next_to_extras(self):
        extra = self.tmp / "extra"
        extra.mkdir()
        prof, rec = self.ensure(False, profile_extra=f'skills = ["core"]\nextra_workspaces = ["{extra}:ro"]')
        create = next(c for c in rec.calls if c[0] == "create")
        core = str((self.sets / "core").resolve())
        self.assertEqual(create[-3:], (str(self.ws), f"{extra}:ro", f"{core}:ro"))
        self.assertEqual(json.loads((prof.state / "skills.mounts").read_text()), [core])

    def test_no_skills_no_extra_mount(self):
        _, rec = self.ensure(False)
        self.assertEqual(next(c for c in rec.calls if c[0] == "create")[-1], str(self.ws))

    def test_refuses_to_mount_a_set_that_broke_since_config_load(self):
        prof = self.config(profile_extra='skills = ["core"]').profile("p")
        self.assertEqual(prof.skill_problems(), [])        # inspected (and cached) while still clean
        (self.sets / "core" / "alpha" / "oops").symlink_to("/etc")
        rec = Recorder()
        with mock.patch.object(ab, "sbx", rec), \
                mock.patch.object(ab, "sandbox_exists", lambda prof, retry=False: False):
            with self.assertRaises(SystemExit):
                ab.ensure_sandbox(prof)
        self.assertEqual(rec.calls, [])

    def test_changed_digest_message_mentions_new_mounts(self):
        prof, _ = self.ensure(False)                       # created without skills
        err = self.capture_info(lambda: self.ensure(True, profile_extra='skills = ["core"]'))
        self.assertIn("profile/config changed", err)
        self.assertIn("skill set paths changed", err)
        self.assertIn("agentbox fresh p", err)

    def test_changed_digest_without_path_change_has_no_mount_note(self):
        self.ensure(False, profile_extra='skills = ["core"]')
        err = self.capture_info(lambda: self.ensure(True, profile_extra='skills = ["core"]\nenv = { A = "b" }'))
        self.assertIn("profile/config changed", err)
        self.assertNotIn("skill set paths", err)

    def capture_info(self, fn):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            fn()
        return buf.getvalue()


class LinkCase(SkillCase):
    """The link script runs for real under sh with HOME in a temp dir (LocalVM)."""

    def setUp(self):
        super().setUp()
        self.vm = LocalVM(self)
        self.skills_dir = self.vm.home / ".claude" / "skills"

    def prof(self, skills='skills = ["core"]', **kw):
        return self.config(profile_extra=skills, **kw).profile("p")

    def manifest(self):
        f = self.skills_dir / ".agentbox-links"
        return f.read_text().split() if f.exists() else None

    def link(self, prof):
        out = []
        with mock.patch.object(ab, "info", out.append):
            ab.sync_skills(prof, background=False)
        return out


class LinkTests(LinkCase):
    def test_links_every_skill_and_records_them(self):
        prof = self.prof()
        self.assertEqual(self.link(prof), [])
        core = (self.sets / "core").resolve()
        for sk in ("alpha", "beta"):
            self.assertEqual(os.readlink(self.skills_dir / sk), f"{core}/{sk}")
            self.assertTrue((self.skills_dir / sk / "SKILL.md").is_file())
        self.assertEqual(sorted(self.manifest()), ["alpha", "beta"])
        self.assertEqual(json.loads((prof.state / "skills.json").read_text()), ["alpha", "beta"])

    def test_idempotent_and_exactly_one_exec(self):
        prof = self.prof()
        self.link(prof)
        before = len(self.vm.calls)
        self.link(prof)
        execs = [c for c in self.vm.calls[before:] if c[0] == "exec"]
        self.assertEqual(len(execs), 1)
        self.assertEqual(sorted(self.manifest()), ["alpha", "beta"])
        out = ab.vm_exec(prof, ab.skills_link_script(prof.skill_links())).stdout
        self.assertEqual(sorted(out.split("\n")[:2]), ["OK alpha", "OK beta"])

    def test_agent_written_skills_are_never_touched(self):
        prof = self.prof()
        mine = self.skills_dir / "mine"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("agent wrote this")
        (self.skills_dir / "alpha").mkdir()                           # a folder where we want a link
        (self.skills_dir / "alpha" / "SKILL.md").write_text("agent's alpha")
        (self.skills_dir / "beta").symlink_to("/somewhere")           # a symlink agentbox did not make
        out = self.link(prof)
        self.assertEqual((mine / "SKILL.md").read_text(), "agent wrote this")
        self.assertEqual((self.skills_dir / "alpha" / "SKILL.md").read_text(), "agent's alpha")
        self.assertEqual(os.readlink(self.skills_dir / "beta"), "/somewhere")
        self.assertEqual(len(out), 2)
        self.assertTrue(any("'alpha'" in m and "folder" in m for m in out))
        self.assertTrue(any("'beta'" in m and "symlink" in m for m in out))
        self.assertIsNone(self.manifest())
        # removing the config later leaves all of it alone
        self.link(self.config(profile_extra="").profile("p"))
        self.assertTrue((mine / "SKILL.md").is_file() and (self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_replaces_its_own_stale_link_and_removes_unconfigured_ones(self):
        prof = self.prof()
        self.link(prof)
        (self.skills_dir / "alpha").unlink()
        (self.skills_dir / "alpha").symlink_to("/old/place")           # ours (listed), wrong target
        self.link(prof)
        self.assertEqual(os.readlink(self.skills_dir / "alpha"), f"{(self.sets / 'core').resolve()}/alpha")
        only_alpha = self.make_set("core", ("alpha",), where=self.tmp / "tiny")
        smaller = self.config({"core": f'path = "{only_alpha}"'}, 'skills = ["core"]').profile("p")
        self.link(smaller)
        self.assertEqual(os.readlink(self.skills_dir / "alpha"), f"{only_alpha}/alpha")
        self.assertFalse(os.path.lexists(self.skills_dir / "beta"))
        self.assertEqual(self.manifest(), ["alpha"])

    def test_dropping_skills_from_config_unlinks_and_forgets(self):
        self.link(self.prof())
        (self.skills_dir / "mine").mkdir()
        self.link(self.config(profile_extra="").profile("p"))
        self.assertEqual(sorted(p.name for p in self.skills_dir.iterdir()), ["mine"])
        self.assertFalse((self.state_dir / "ab-p" / "skills.json").exists())

    def test_agent_replacing_a_link_with_a_folder_keeps_the_folder(self):
        prof = self.prof()
        self.link(prof)
        (self.skills_dir / "alpha").unlink()
        (self.skills_dir / "alpha").mkdir()
        (self.skills_dir / "alpha" / "SKILL.md").write_text("mine now")
        self.link(prof)
        self.link(self.config(profile_extra="").profile("p"))
        self.assertEqual((self.skills_dir / "alpha" / "SKILL.md").read_text(), "mine now")

    def test_forged_manifest_cannot_delete_anything(self):
        victim = self.vm.home / "victim"
        victim.mkdir()
        (victim / "keep").write_text("x")
        self.skills_dir.mkdir(parents=True)
        (self.skills_dir / "real-folder").mkdir()
        (self.skills_dir / ".agentbox-links").write_text(
            "../../victim\n/etc\n*\n.claude\nreal-folder\n-rf\n$(touch pwned)\n`touch pwned`\n")
        prof = self.prof()
        self.link(prof)
        self.assertTrue((victim / "keep").exists())
        self.assertTrue((self.skills_dir / "real-folder").is_dir())
        self.assertFalse((self.vm.home / "pwned").exists())
        self.assertFalse((self.skills_dir / "pwned").exists())
        self.assertTrue((self.vm.home / ".claude").is_dir())

    def test_unmounted_skill_is_not_linked(self):
        prof = self.prof()
        gone = prof.skill_links()[0][1]
        (self.sets / "core" / "alpha" / "SKILL.md").unlink()           # the VM would see no SKILL.md
        out = ab.vm_exec(prof, ab.skills_link_script(prof.skill_links())).stdout
        self.assertIn("SKIP alpha not-mounted", out)
        self.assertFalse(os.path.lexists(self.skills_dir / "alpha"))
        self.assertTrue(gone.endswith("/alpha"))

    def test_script_quotes_odd_paths(self):
        root = self.make_set("odd", ("alpha",), where=self.tmp / "it's a dir; touch pwned")
        prof = self.config({"core": f'path = "{root}"'}, 'skills = ["core"]').profile("p")
        self.link(prof)
        self.assertEqual(os.readlink(self.skills_dir / "alpha"), f"{root}/alpha")
        self.assertFalse((self.vm.home / "pwned").exists())


class SyncTests(SkillCase):
    def test_profile_without_skills_makes_no_sbx_call(self):
        prof = self.config().profile("p")
        ab.sync_skills(prof)                      # sbx is forbidden by the test base class

    def test_stopped_sandbox_is_never_booted(self):
        vm = LocalVM(self, vm="stopped")
        prof = self.config(profile_extra='skills = ["core"]').profile("p")
        started = []
        out = []
        with mock.patch.object(ab.threading, "Thread", lambda **kw: mock.Mock(start=lambda: started.append(kw))), \
                mock.patch.object(ab, "info", out.append):
            ab.sync_skills(prof)
            ab.sync_skills(prof, background=False)
        self.assertEqual(len(started), 1)                      # only the background form waits
        self.assertTrue(started[0]["daemon"])
        self.assertFalse([c for c in vm.calls if c[0] == "exec"])
        self.assertTrue(any("not running" in m for m in out))

    def test_waiter_links_once_the_vm_comes_up_and_never_boots_it(self):
        vm = LocalVM(self, vm="stopped")
        prof = self.config(profile_extra='skills = ["core"]').profile("p")
        polls = []

        def sleep(_):
            polls.append(vm.vm)
            if len(polls) == 2:
                vm.vm = "running"
        with mock.patch.object(ab.time, "sleep", sleep):
            ab.link_skills_when_running(prof, timeout=60, interval=1)
        self.assertEqual(len(polls), 2)
        self.assertTrue((vm.home / ".claude" / "skills" / "alpha").is_symlink())

    def test_waiter_gives_up_when_the_sandbox_is_gone_or_never_starts(self):
        vm = LocalVM(self, exists=False)
        prof = self.config(profile_extra='skills = ["core"]').profile("p")
        ab.link_skills_when_running(prof)
        vm.exists, vm.vm = True, "stopped"
        with mock.patch.object(ab.time, "sleep", lambda s: None), \
                mock.patch.object(ab.time, "monotonic", mock.Mock(side_effect=[0, 1, 2, 999])):
            ab.link_skills_when_running(prof, timeout=10)
        self.assertFalse([c for c in vm.calls if c[0] == "exec"])


class CommandTests(LinkCase):
    def test_run_links_before_the_job_starts(self):
        prof = self.prof()
        for name, fn in (("start_keepalive", lambda p: None), ("ensure_sandbox", lambda p, retry=False: None)):
            m = mock.patch.object(ab, name, fn)
            m.start()
            self.addCleanup(m.stop)
        args = argparse.Namespace(prompt="hi", prompt_file=None, detach=True, agent_args=[], keepalive=None)
        with mock.patch.object(ab, "print", create=True):
            self.assertEqual(ab.cmd_run(prof, args), 0)
        self.assertTrue((self.skills_dir / "alpha").is_symlink())

    def test_up_and_shell_and_resume_sync(self):
        prof = self.prof()
        seen = []
        with mock.patch.object(ab, "ensure_sandbox", lambda p, retry=False: None), \
                mock.patch.object(ab, "sync_skills", lambda p, background=True: seen.append(("sync", background))), \
                mock.patch.object(ab, "sbx", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")):
            with mock.patch.object(ab, "print", create=True):
                ab.cmd_up(prof, argparse.Namespace(agent_args=[]))
        self.assertEqual(seen, [("sync", True)])


class StatusDoctorTests(LinkCase):
    def test_status_json_lists_skill_sets(self):
        self.config(profile_extra='skills = ["core"]')
        FakeSbx(exists=True, names=("ab-p",)).install(self)
        code, out, _ = self.run_main("status", "--json")
        self.assertEqual(code, 0)
        row = json.loads(out)["profiles"][0]
        self.assertEqual(row["skills"], [{"set": "core", "path": str((self.sets / "core").resolve()),
                                          "ref": None, "commit": None, "branch": None,
                                          "skills": ["alpha", "beta"], "drift": "unrecorded"}])

    def test_status_json_without_skills_is_an_empty_list(self):
        self.config()
        FakeSbx(exists=False).install(self)
        _, out, _ = self.run_main("status", "--json")
        self.assertEqual(json.loads(out)["profiles"][0]["skills"], [])

    def doctor(self, vm_state="running"):
        self.vm.vm = vm_state
        orig = type(self.vm).__call__

        def call(vm, cfg, *a, **k):
            if a[0] == "version":
                return subprocess.CompletedProcess(a, 0, "sbx 9\n", "")
            if a[:2] == ("kit", "validate"):
                return subprocess.CompletedProcess(a, 0, "", "")
            if a[0] == "exec" and "ssh-add" in a[-1]:
                return subprocess.CompletedProcess(a, 2, "Could not open a connection to your authentication agent.", "")
            if a[0] == "exec" and "TOKENVAR" in a[-1]:
                return subprocess.CompletedProcess(a, 0, "hosttool=absent\n", "")
            return orig(vm, cfg, *a, **k)
        with mock.patch.object(type(self.vm), "__call__", call):
            code, out, err = self.run_main("doctor", "p", "--json")
        return code, json.loads(out), err

    def test_doctor_reports_sets_and_link_state(self):
        self.config(profile_extra='skills = ["core"]')
        code, doc, _ = self.doctor()
        msgs = {c["message"]: c["status"] for c in doc["checks"]}
        self.assertEqual(code, 1)                                  # not linked yet
        self.assertEqual(msgs["skill alpha is not linked (not-linked)"], "fail")
        self.assertTrue(any("2 skill(s): alpha, beta; ref none; not a git repo; drift: unrecorded" in m for m in msgs))
        self.assertEqual(msgs[f"skill sets pass the safety rules: core"], "pass")
        ab.sync_skills(self.config(profile_extra='skills = ["core"]').profile("p"), background=False)
        code, doc, _ = self.doctor()
        self.assertEqual(code, 0, doc)
        self.assertIn("skill alpha is linked to the mounted set", [c["message"] for c in doc["checks"]])

    def test_doctor_fails_on_a_rule_violation_and_a_wrong_link(self):
        root = self.make_set()
        (root / "alpha" / "run.sh").write_text("echo")
        self.config({"core": f'path = "{root}"'}, 'skills = ["core"]')
        code, doc, _ = self.doctor(vm_state="stopped")
        fails = [c["message"] for c in doc["checks"] if c["status"] == "fail"]
        self.assertEqual(code, 1)
        self.assertTrue(any("skill set core" in m and "alpha/run.sh" in m for m in fails), fails)

    def test_doctor_notices_a_link_pointing_elsewhere(self):
        self.config(profile_extra='skills = ["core"]')
        self.skills_dir.mkdir(parents=True)
        (self.skills_dir / "alpha").symlink_to("/elsewhere")
        _, doc, _ = self.doctor()
        self.assertIn("skill alpha is not linked (points-elsewhere)",
                      [c["message"] for c in doc["checks"] if c["status"] == "fail"])


if __name__ == "__main__":
    unittest.main()
