import unittest

from helpers import AgentboxTestCase, ab


class GitConfigAuditTests(AgentboxTestCase):
    def repo(self, config_text):
        git = self.ws / ".git"
        git.mkdir(exist_ok=True)
        (git / "config").write_text(config_text)
        return self.ws

    def test_flags_commands_and_redirects(self):
        root = self.repo('''
[core]
    repositoryformatversion = 0
    fsmonitor = /tmp/evil
    hooksPath = .githooks
[alias]
    st = !sh -c 'id'
[remote "origin"]
    url = https://example.com/x.git
    uploadpack = /tmp/up
[filter "lfs"]
    smudge = /tmp/smudge %f
[diff "x"]
    textconv = /tmp/tc
[credential]
    helper = !/tmp/steal
[url "ssh://evil/"]
    insteadOf = https://example.com/
''')
        findings = "\n".join(ab.risky_git_config(root))
        for needle in ("core.fsmonitor", "core.hookspath", "alias.st", "remote.origin.uploadpack",
                       "filter.lfs.smudge", "diff.x.textconv", "credential.helper", "insteadof"):
            self.assertIn(needle, findings, needle)

    def test_benign_config_has_no_findings(self):
        root = self.repo('''
[core]
    repositoryformatversion = 0
    filemode = true
[remote "origin"]
    url = https://example.com/x.git
    fetch = +refs/heads/*:refs/remotes/origin/*
[branch "main"]
    remote = origin
''')
        self.assertEqual(ab.risky_git_config(root), [])

    def test_one_line_section_and_include_are_followed(self):
        inc = self.tmp / "extra.cfg"
        inc.write_text("[core]\n    sshCommand = /tmp/ssh\n")
        root = self.repo(f"[core] fsmonitor = x\n[include]\n    path = {inc}\n")
        findings = "\n".join(ab.risky_git_config(root))
        self.assertIn("core.fsmonitor", findings)
        self.assertIn("core.sshcommand", findings)
        self.assertIn("include.path", findings)

    def test_symlinked_and_file_dot_git_are_reported(self):
        (self.ws / ".git").symlink_to(self.tmp)
        self.assertTrue(any(".git is a symlink" in f for f in ab.risky_git_config(self.ws)))

    def test_no_repo_no_findings(self):
        self.assertEqual(ab.risky_git_config(self.ws), [])

    def test_scan_records_symlinks_without_following(self):
        (self.ws / "Makefile").symlink_to("/etc/passwd")
        (self.ws / "CMakeLists.txt").write_text("project(x)\n")
        found = ab._scan(self.ws, ["Makefile", "CMakeLists.txt"], [])
        self.assertEqual(found["Makefile"], "symlink:/etc/passwd")
        self.assertEqual(len(found["CMakeLists.txt"]), 64)


if __name__ == "__main__":
    unittest.main()
