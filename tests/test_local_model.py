"""`local_model`: opencode in the sandbox on a model served by Ollama on the host."""
import contextlib
import io
import json
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from helpers import AgentboxTestCase, FakeSbx, ab

LM = 'local_model = { provider = "ollama", model = "gemma4:12b", port = 11434 }'


class ValidationTests(AgentboxTestCase):
    def cfg(self, local_model=LM, agent="opencode", extra=""):
        return self.write_config(f'''
            [profiles.tools]
            workspace = "{self.ws}"
            host_tools_port = 8765
            [profiles.lm]
            workspace = "{self.ws}"
            agent = "{agent}"
            {local_model}
            {extra}
        ''')

    def problems(self, cfg):
        return ab.local_model_problems(cfg, cfg.merged("lm"))

    def test_a_good_entry_loads_with_defaults_filled_in(self):
        prof = self.cfg().profile("lm")
        self.assertEqual(prof.local_model, {"provider": "ollama", "model": "gemma4:12b", "port": 11434, "context": 32768})
        prof = self.cfg('local_model = { provider = "ollama", model = "hf.co/u/r:Q4_K_M", context = 65536 }').profile("lm")
        self.assertEqual((prof.local_model["port"], prof.local_model["context"]), (11434, 65536))

    def test_absent_means_none_and_no_problems(self):
        cfg = self.cfg(local_model="", agent="claude")
        self.assertIsNone(cfg.profile("lm").local_model)
        self.assertEqual(self.problems(cfg), [])
        self.assertNotIn("local_model", cfg.profile("lm").d)      # existing sandboxes' kit digest is unchanged

    def test_model_names_with_shell_metacharacters_are_refused(self):
        for bad in ("gemma;rm -rf ~", "$(id)", "a b", "-m", "x`y`", "model'", ":tag", "a/" * 6 + "b", ""):
            cfg = self.cfg(f'local_model = {{ provider = "ollama", model = {json.dumps(bad)} }}')
            self.assertTrue(any("local_model.model" in p for p in self.problems(cfg)), bad)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                cfg.profile("lm")

    def test_provider_port_context_and_keys(self):
        for lm, word in (('{ provider = "lmstudio", model = "m" }', "provider"),
                         ('{ provider = "ollama", model = "m", port = 0 }', "port"),
                         ('{ provider = "ollama", model = "m", port = 70000 }', "port"),
                         ('{ provider = "ollama", model = "m", port = "11434" }', "port"),
                         ('{ provider = "ollama", model = "m", port = true }', "port"),
                         ('{ provider = "ollama", model = "m", context = 100 }', "context"),
                         ('{ provider = "ollama", model = "m", url = "http://evil" }', "unknown key")):
            problems = self.problems(self.cfg(f"local_model = {lm}"))
            self.assertTrue(any(word in p for p in problems), (lm, problems))
        self.assertIn("must be a table", self.problems(self.cfg('local_model = "gemma4"'))[0])

    def test_port_must_not_be_any_profiles_host_tools_port(self):
        problems = self.problems(self.cfg('local_model = { provider = "ollama", model = "m", port = 8765 }'))
        self.assertTrue(any("host_tools_port in tools" in p for p in problems), problems)
        cfg = self.write_config(f'''
            [defaults]
            host_tools_port = 9000
            [profiles.lm]
            workspace = "{self.ws}"
            agent = "opencode"
            local_model = {{ provider = "ollama", model = "m", port = 9000 }}
        ''')
        self.assertTrue(any("defaults" in p for p in self.problems(cfg)))

    def test_other_agents_are_refused_with_a_clear_message(self):
        for agent in ("claude", "codex", "shell"):
            problems = self.problems(self.cfg(agent=agent))
            self.assertEqual(len(problems), 1)
            self.assertIn('only supported with agent = "opencode"', problems[0])

    def test_doctor_reports_problems_instead_of_crashing(self):
        self.cfg(agent="claude")
        FakeSbx().install(self)
        code, out, _ = self.run_main("doctor", "lm", "--json")
        self.assertEqual(code, 1)
        fails = [c for c in json.loads(out)["checks"] if c["status"] == "fail"]
        self.assertEqual(len(fails), 1)
        self.assertIn("opencode", fails[0]["message"])


class KitTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'''
            [profiles.lm]
            workspace = "{self.ws}"
            agent = "opencode"
            allow = ["example.com"]
            {LM}
            [profiles.plain]
            workspace = "{self.ws}"
            agent = "opencode"
        ''')
        self.cfg = ab.Config()

    def render(self, name):
        kit_dir, digest = ab.render_local_kit(self.cfg.profile(name), "tok")
        return Path(kit_dir), (Path(kit_dir) / "spec.yaml").read_text()

    def test_allow_entry_env_and_provider_config(self):
        kit, spec = self.render("lm")
        self.assertIn('- "localhost:11434"', spec)
        self.assertIn('- "example.com"', spec)
        self.assertIn(f'OPENCODE_CONFIG: "{ab.OPENCODE_CONFIG_VM}"', spec)
        conf = json.loads((kit / "files" / "home" / ".agentbox" / "opencode.json").read_text())
        prov = conf["provider"]["ollama"]
        self.assertEqual(prov["npm"], "@ai-sdk/openai-compatible")
        self.assertEqual(prov["options"]["baseURL"], "http://host.docker.internal:11434/v1")
        self.assertEqual(prov["models"]["gemma4:12b"]["limit"], {"context": 32768, "output": 8192})
        self.assertTrue(prov["models"]["gemma4:12b"]["tool_call"])
        self.assertEqual((conf["model"], conf["small_model"]), ("ollama/gemma4:12b", "ollama/gemma4:12b"))
        self.assertEqual(ab.OPENCODE_CONFIG_VM, "/home/agent/.agentbox/opencode.json")   # files/home/ -> /home/agent/

    def test_nothing_is_written_into_the_workspace(self):
        before = sorted(self.ws.rglob("*"))
        kit, _ = self.render("lm")
        self.assertEqual(sorted(self.ws.rglob("*")), before)
        self.assertTrue(ab.is_within(kit, self.state_dir))

    def test_a_profile_without_it_opens_no_host_port(self):
        kit, spec = self.render("plain")
        self.assertNotIn("localhost:", spec)
        self.assertNotIn("OPENCODE_CONFIG", spec)
        self.assertFalse((kit / "files" / "home" / ".agentbox" / "opencode.json").exists())

    def test_changing_the_model_changes_the_digest(self):
        _, d1 = ab.render_local_kit(self.cfg.profile("lm"), "tok")
        conf = self.cfg_dir / "agentbox.toml"
        conf.write_text(conf.read_text().replace("gemma4:12b", "qwen3:8b"))
        _, d2 = ab.render_local_kit(ab.Config().profile("lm"), "tok")
        self.assertNotEqual(d1, d2)


class FakeOllama:
    """Stand-in for ab.ollama_api: answers from dicts, or raises like an unreachable server."""

    def __init__(self, models=("gemma4:12b",), caps=("completion", "tools"), native=131072, loaded=None,
                 params="", up=True):
        self.models, self.caps, self.native, self.loaded, self.params, self.up = models, caps, native, loaded, params, up
        self.calls = []

    def __call__(self, port, path, body=None, timeout=3.0):
        self.calls.append((port, path, body))
        if not self.up:
            raise ConnectionRefusedError(61, "Connection refused")
        if path == "/api/tags":
            return {"models": [{"name": m} for m in self.models]}
        if path == "/api/show":
            caps = ["embedding"] if "embed" in body["model"] else self.caps
            return {"capabilities": list(caps), "parameters": self.params,
                    "model_info": {"gemma3.context_length": self.native}}
        if path == "/api/ps":
            return {"models": [{"name": "gemma4:12b", "context_length": self.loaded}] if self.loaded else []}
        raise AssertionError(path)

    def install(self, case):
        p = mock.patch.object(ab, "ollama_api", self)
        p.start()
        case.addCleanup(p.stop)
        return self


class DoctorTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.lm]\nworkspace = "{self.ws}"\nagent = "opencode"\n{LM}\n')
        FakeSbx().install(self)

    def doctor(self, **kw):
        FakeOllama(**kw).install(self)
        code, out, _ = self.run_main("doctor", "lm", "--json")
        checks = [c for c in json.loads(out)["checks"] if c["profile"] == "lm"]
        return code, checks

    def test_all_good(self):
        code, checks = self.doctor(loaded=32768)
        self.assertEqual(code, 0, checks)
        msgs = [c["message"] for c in checks if c["status"] == "pass"]
        for want in ("Ollama answers on 127.0.0.1:11434", "Ollama has gemma4:12b", "gemma4:12b supports tool calls"):
            self.assertIn(want, msgs)
        self.assertFalse([c for c in checks if c["status"] == "warn"])

    def test_ollama_down(self):
        code, checks = self.doctor(up=False)
        self.assertEqual(code, 1)
        fail = [c for c in checks if c["status"] == "fail"]
        self.assertEqual(len(fail), 1)
        self.assertIn("start Ollama", fail[0]["hint"])

    def test_model_missing_and_no_tools(self):
        code, checks = self.doctor(models=("llama3:8b",))
        self.assertEqual(code, 1)
        self.assertIn("ollama pull gemma4:12b", [c for c in checks if c["status"] == "fail"][0]["hint"])
        code, checks = self.doctor(caps=("completion",))
        self.assertEqual(code, 1)
        self.assertEqual([c["message"] for c in checks if c["status"] == "fail"], ["gemma4:12b supports tool calls"])

    def test_context_warnings_explain_the_ollama_setting(self):
        _, checks = self.doctor(loaded=4096)
        warn = [c for c in checks if c["status"] == "warn"]
        self.assertEqual(len(warn), 1)
        self.assertIn("4096", warn[0]["message"])
        self.assertIn("OLLAMA_CONTEXT_LENGTH", warn[0]["hint"])
        _, checks = self.doctor(params="num_ctx 8192\nstop <eos>", native=8192)
        self.assertEqual(len([c for c in checks if c["status"] == "warn"]), 2)     # the model's own max + Ollama's
        _, checks = self.doctor()                                                   # not loaded: we can't tell
        self.assertTrue(any("OLLAMA_CONTEXT_LENGTH" in c.get("hint", "") for c in checks if c["status"] == "info"))

    def test_context_helpers_ignore_junk(self):
        self.assertEqual(ab.ollama_context(None, None, "m"), (None, None))
        self.assertEqual(ab.ollama_context({"model_info": {"x.context_length": "big"}, "parameters": 5},
                                           {"models": "no"}, "m"), (None, None))
        self.assertEqual(ab.ollama_capabilities({"capabilities": ["tools", 3]}), ["tools"])
        self.assertTrue(ab.ollama_has_model(["qwen3:latest"], "qwen3"))
        self.assertFalse(ab.ollama_has_model(["qwen3:8b"], "qwen3"))


class ProvisionTests(AgentboxTestCase):
    def setUp(self):
        super().setUp()
        self.write_config(f'[profiles.old]\nworkspace = "{self.ws}"\nhost_tools_port = 8765\n')
        self.conf = self.cfg_dir / "agentbox.toml"
        FakeSbx(exists=False).install(self)
        p = mock.patch.object(sys, "stdin", io.StringIO())
        p.start()
        self.addCleanup(p.stop)

    def test_flags_write_a_local_model_that_reads_back(self):
        FakeOllama(models=("gemma4:12b", "nomic-embed"), loaded=32768).install(self)
        code, out, err = self.run_main("provision", "loc", "--yes", "--workspace", str(self.ws), "--agent", "opencode",
                                       "--local-model", "gemma4:12b")
        self.assertEqual(code, 0, out + err)
        p = tomllib.loads(self.conf.read_text())["profiles"]["loc"]
        self.assertEqual(p["local_model"], {"provider": "ollama", "model": "gemma4:12b", "port": 11434})
        self.assertIn("suggest gemma4:12b", out)
        self.assertNotIn("suggest nomic-embed", out)                 # no tools capability
        self.assertIn("Ollama has gemma4:12b", out)                  # doctor ran with the new profile

    def test_refused_for_other_agents_and_on_a_host_tools_port(self):
        FakeOllama().install(self)
        code, _, err = self.run_main("provision", "loc", "--yes", "--workspace", str(self.ws), "--agent", "claude",
                                     "--local-model", "gemma4:12b")
        self.assertEqual(code, 2)
        self.assertIn("--local-model works with --agent opencode", err)
        code, _, err = self.run_main("provision", "loc", "--yes", "--workspace", str(self.ws), "--agent", "opencode",
                                     "--local-model", "gemma4:12b", "--local-model-port", "8765")
        self.assertEqual(code, 2)
        self.assertIn("host_tools_port in old", err)
        code, _, err = self.run_main("provision", "loc", "--yes", "--workspace", str(self.ws), "--agent", "opencode",
                                     "--local-model", "x;id")
        self.assertEqual(code, 2)
        self.assertNotIn("loc", tomllib.loads(self.conf.read_text())["profiles"])

    def test_yes_without_the_flag_adds_no_local_model(self):
        code, out, err = self.run_main("provision", "loc", "--yes", "--workspace", str(self.ws), "--agent", "opencode")
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("local_model", tomllib.loads(self.conf.read_text())["profiles"]["loc"])


if __name__ == "__main__":
    unittest.main()
