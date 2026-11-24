"""`agentbox template`: prebuild a profile's kits into an sbx template, and create sandboxes from it."""
import contextlib
import io
import json
import subprocess

from helpers import AgentboxTestCase, ab


class Recorder:
    def __init__(self, fail=()):
        self.calls, self.fail = [], fail
        self.images = None          # `template ls --json` answer; None: unreadable (as before the check)

    def __call__(self, cfg, *args, check=True, capture=False, stdin=None):
        self.calls.append(args)
        rc = 1 if args[0] in self.fail else 0
        if rc and check:
            raise SystemExit(1)
        out = json.dumps(self.images) if args[:2] == ("template", "ls") and self.images is not None else ""
        return subprocess.CompletedProcess(args, rc, out, "")


class TemplateTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        kit = self.cfg_dir / "kits" / "tools"
        kit.mkdir(parents=True)
        (kit / "tools.yaml").write_text(ab.KIT_SYNTAX + "\nkind: mixin\n")
        self.kit = kit
        self.write_config(f'''
            [profiles.p]
            workspace = "{self.ws}"
            kits = ["tools"]
            [profiles.bare]
            workspace = "{self.ws}"
        ''')
        self.rec = Recorder()
        self.patch("sbx", self.rec)

    def patch(self, name, value):
        from unittest import mock
        p = mock.patch.object(ab, name, value)
        p.start()
        self.addCleanup(p.stop)

    def prof(self, name="p"):
        return ab.Config().profile(name)

    def build(self, name="p"):
        args = type("A", (), {"remove": False})()
        with contextlib.redirect_stderr(io.StringIO()):
            return ab.cmd_template(self.prof(name), args)

    def test_build_uses_only_the_agent_and_kits(self):
        self.assertEqual(self.build(), 0)
        create = next(c for c in self.rec.calls if c[0] == "create")
        self.assertEqual(create[:4], ("create", "--name", "ab-p-tpl", "--kit"))
        self.assertEqual(create[4], str(self.kit))
        self.assertEqual(create[5], "claude")
        self.assertNotIn(str(self.ws), create)                    # never the real workspace
        self.assertFalse(any("agentbox" in str(a) and "kit" in str(a) for a in create[4:]))   # no generated kit
        kinds = [c[:2] for c in self.rec.calls]
        self.assertLess(kinds.index(("stop", "ab-p-tpl")), kinds.index(("template", "save")))
        self.assertEqual(self.rec.calls[-1][:2], ("rm", "--force"))   # the build box goes
        rec = ab.template_record(self.prof())
        self.assertTrue(ab.TEMPLATE_TAG_RE.match(rec["tag"]), rec)
        self.assertEqual(ab.built_template(self.prof()), rec["tag"])

    def test_a_kit_change_makes_it_stale(self):
        self.build()
        (self.kit / "tools.yaml").write_text(ab.KIT_SYNTAX + "\nkind: mixin\n# changed\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(ab.built_template(self.prof()))
        self.assertIn("agentbox template p", err.getvalue())

    def test_failed_build_records_nothing_and_cleans_up(self):
        self.rec.fail = ("template",)
        with self.assertRaises(SystemExit):
            self.build()
        self.assertIsNone(ab.template_record(self.prof()))
        self.assertEqual(self.rec.calls[-1], ("rm", "--force", "ab-p-tpl"))

    def test_rebuild_removes_the_old_image_and_remove_deletes_it(self):
        self.build()
        path = ab.template_record_path(self.prof())
        old = json.loads(path.read_text())
        path.write_text(json.dumps({**old, "tag": "agentbox-p:20000101000000"}))
        self.build()
        self.assertIn(("template", "rm", "--force", "agentbox-p:20000101000000"), self.rec.calls)
        tag = ab.template_record(self.prof())["tag"]
        with contextlib.redirect_stderr(io.StringIO()):
            ab.cmd_template(self.prof(), type("A", (), {"remove": True})())
        self.assertIn(("template", "rm", "--force", tag), self.rec.calls)
        self.assertFalse(path.exists())

    def test_a_tampered_record_is_ignored(self):
        path = ab.template_record_path(self.prof())
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"tag": "--privileged", "fingerprint": ab.template_fingerprint(self.prof())}))
        self.assertIsNone(ab.built_template(self.prof()))

    def test_refusals(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.build("bare")                                    # no kits: nothing to prebuild
        self.assertEqual(self.rec.calls, [])


class CreateFromTemplateTests(TemplateTests):
    def create_args(self):
        for name in ("write_snapshot", "start_bridge", "set_secrets", "record_skill_heads", "restore_sessions"):
            self.patch(name, lambda prof: None)
        self.patch("sandbox_exists", lambda prof, retry=False: False)
        self.rec.calls.clear()
        with contextlib.redirect_stderr(io.StringIO()):
            ab.ensure_sandbox(self.prof())
        return next(c for c in self.rec.calls if c[0] == "create")

    def test_a_current_template_is_used(self):
        self.build()
        tag = ab.template_record(self.prof())["tag"]
        create = self.create_args()
        i = create.index("--template")
        self.assertEqual(create[i:i + 4], ("--template", tag, "--pull", "never"))

    def test_without_one_nothing_changes(self):
        self.assertNotIn("--template", self.create_args())

    def test_a_stale_one_is_not_used(self):
        self.build()
        (self.kit / "tools.yaml").write_text(ab.KIT_SYNTAX + "\n# changed\n")
        self.assertNotIn("--template", self.create_args())

    def listing(self, tag):
        repo, _, t = tag.partition(":")
        return [{"repository": "docker.io/docker/sandbox-templates", "tag": "claude-code-docker"},
                {"repository": f"docker.io/library/{repo}", "tag": t}]

    def test_a_template_in_the_image_store_is_used(self):
        self.build()
        tag = ab.template_record(self.prof())["tag"]
        self.rec.images = self.listing(tag)
        self.assertIn(tag, self.create_args())

    def test_a_template_gone_from_the_image_store_is_dropped(self):
        self.build()
        tag = ab.template_record(self.prof())["tag"]
        self.rec.images = self.listing("agentbox-p:20000101000000")       # a different build is there
        self.assertNotIn("--template", self.create_args())
        self.assertIsNone(ab.template_record(self.prof()))               # not checked again on every up
        self.assertNotIn(tag, [a for c in self.rec.calls for a in c])
