# Working notes: agents, sandboxes and review

How agents work on the owner's repos (agentbox itself, VoiceDev, ...): inside an agentbox sandbox, never directly on the host. These notes record what we
learned on 2026-10-31. The README is the full reference.

## Ground rules

- **Changes happen inside a sandbox.** On the host, an assistant may only read non-sensitive files, and run read-only
  commands (`git log`, `git diff`, `git status`, `sbx ls`).
- **Ask the owner first** before any of these on the host:
  - editing files, git merges, resets or fetches, worktrees;
  - installs, test runs or scripts;
  - agentbox commands that change state (`run`, `up`, `down`, `fresh`, `fetch`);
  - reading secrets, tokens, keys or `.env` files.
  Several steps can be asked about together in one question.
- **The owner merges, installs and pushes.** An assistant stops at "ready on branch X" and gives the commands.
- **No push credentials and no SSH agent in the VMs.** Never put secret values on a command line.

## The loop

Profiles such as `voicedev` and `agentbox` use clone mode: the agent works on its own clone, and the host tree is untouched until you fetch.

```bash
agentbox run voicedev --prompt-file task.md --detach   # the brief goes in a file (stdin), never on argv
agentbox progress voicedev --watch                     # measured numbers + the agent's own plan
agentbox wait voicedev --timeout 3600                  # exit status is the job's
agentbox fetch voicedev                                # commits into review/voicedev; never merges
git diff HEAD...review/voicedev                        # review
git merge --ff-only review/voicedev                    # owner only
```

## sbx behaviour worth knowing (sbx 0.50.1)

- **Idle stop:** sbx stops an idle sandbox about 30 s after the last `exec`/`run` session ends. agentbox's keep-alive
  protects running jobs.
- **The git remote is temporary:** sbx adds `sandbox-ab-<profile>` to the host repo only while the sandbox runs, and
  removes it when the sandbox stops. `agentbox fetch` keeps the sandbox up while it fetches. Commits fetched before
  removal stay under `refs/sandboxes/ab-<profile>/*`; keep one with `git branch NAME refs/sandboxes/...`.
- **Booting relaunches the agent:** a stopped VM that boots restarts the agent with its last `sbx run` arguments. Even
  `sbx exec` starts a stopped sandbox, which is why agentbox never boots one unless asked (`--start`).
- **Mounts are fixed at creation.** A new mount, for example a skill set added to a profile, needs `agentbox fresh`.
  Without a terminal, `down`/`fresh` need `--force`, and that deletes the sandbox even while VS Code is connected.
- **The network allow-list is not binding yet:** the global sbx policy is "Balanced" (about 195 allows), and a
  profile's `allow` only adds to it. Tracked in #1.
- **Credentials in the VMs:**
  - `GH_TOKEN` in the VMs is a placeholder; GitHub returns 401 for it. `SSH_AUTH_SOCK` exists, but `ssh-add -l` fails.
  - The VMs can reach `mcp-proxy.anthropic.com`, probably claude.ai connectors (#4).
- **Claude Code in the VM** is 2.1.306. Headless `-p` has no TodoWrite. agentbox's `jobprogress` gives the plan instead.

## Skills

- **Where they come from:** skills come from `~/gits/skills` (the owner's fork of mattpocock/skills). It's mounted
  read-only, and pinned by `ref` in `[skillsets.pocock]`.
- **What `voicedev` and `agentbox` get:** both profiles have 10 of them:
  - `sandbox` (our own, in `skills/local/`)
  - `tdd`, `diagnosing-bugs`, `codebase-design`, `to-spec`, `domain-modeling`
  - `grill-me`, `grilling`, `handoff`, `wait-what`
- **The security review:** the review of the upstream skills is on the skills repo's branch `review/security-review`
  (`SECURITY-REVIEW.md`). Never run that repo's `scripts/link-skills.sh`.
- **Updating:** fetch upstream, diff the included paths, merge, then update `ref`. The mount is live, so a checkout
  in `~/gits/skills` changes what every sandbox sees.

## Open agentbox issues

On GitHub:

1. #1 Locked Down policy
2. #2 Global sbx hardening
3. #3 `agentbox watch` monitoring
4. #4 claude.ai MCP connectors in the VM
