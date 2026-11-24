# Working notes: agents, sandboxes and review

How agents work on the owner's repos (agentbox itself, VoiceDev, ...): inside an agentbox sandbox, never directly on the host. These notes record what we
learned on 2026-10-31. The README is the full reference.

## Ground rules

- **Changes happen inside a sandbox.** On the host, an assistant may only read non-sensitive files, and run read-only
  commands (`git log`, `git diff`, `git status`, `sbx ls`).
- **Ask the owner first** before any of these on the host:
  - editing files, git merges, resets or fetches, worktrees;
  - installs, test runs or scripts;
  - agentbox commands that change state (`run`, `up`, `down`, `fresh`, `fetch`, `template`);
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
  `sbx exec` starts a stopped sandbox, which is why agentbox never boots one unless asked (`--start`). The exception
  is `down`/`fresh`, which boot it just before deleting it: to check clone mode for unfetched work, and for `fresh`
  to copy Claude's sessions out.
- **Mounts are fixed at creation.** A new mount, for example a skill set added to a profile, needs `agentbox fresh`.
  Without a terminal, `down`/`fresh` need `--force`, and that deletes the sandbox even while VS Code is connected.
- **The network allow-list is not binding yet:** the global sbx policy is "Balanced" (about 195 allows), and a
  profile's `allow` only adds to it. Tracked in #1.
- **Credentials in the VMs:**
  - `GH_TOKEN` in the VMs is a placeholder; GitHub returns 401 for it. `SSH_AUTH_SOCK` exists, but `ssh-add -l` fails.
  - The VMs can reach `mcp-proxy.anthropic.com`, probably claude.ai connectors (#4).
- **Claude Code in the VM** is 2.1.306. Headless `-p` has no TodoWrite. agentbox's `jobprogress` gives the plan instead.

## Kits v3 and sbx 0.54 (2026-11-24)

- **sbx ships its agents as v3 kits** (`claude` resolves to `docker.io/sbx/claude:latest`) and refuses to compose v2
  kits with them, so every kit, the generated one included, is v3: `<kit>/<kit>.yaml`, first line
  `# syntax=docker/sandbox-kit:3`. Spec and migration skill: github.com/docker/sandbox-kit-spec.
- **sbx reads `spec.yaml` first.** A kit folder that still holds one is v2, even beside a v3 descriptor.
- **Declaration-only mixins need no build** (no Docker, no builder sandbox). Things that turned out to force a
  build anyway: `licenses:`. A kit with a recipe (`junie.dockerfile`) is built on first use, by Docker on the host
  or the `sbx-kit-builder` sandbox (about 2 minutes the first time).
- **`sbx kit validate` rejects v3 source kits**, `--offline` or not; `sbx kit inspect <dir>` loads (and builds) one.
- **Not supported yet by 0.54:** `agent-context@2` `delivery: inline` (so kit instructions are a per-kit file the
  agent is pointed at, not inline in `CLAUDE.md`), and keeping `volume@1` storage across `sbx rm` (a re-created
  sandbox got an empty volume, with or without `size`). That is why `fresh` copies sessions itself.
- **Runtime files land after install hooks**, as the agent user: put things into root-owned places with a root
  startup hook (agentbox installs `hosttool` that way). Hook environments are deny-by-default: list the proxy
  variables in `env:`.
- **Templates work with v3 kits:** `sbx template save` (the sandbox must be stopped), then `create --template TAG
  --pull never`. A C++/JUCE profile went from 64 s to 6 s per sandbox (`agentbox template`).
- **Existing v2-era sandboxes keep working;** kits only matter at creation. `sbx kit add` can re-create a sandbox
  keeping its volumes and clone, but only for sandboxes created recently enough.
- **Claude Code in the VM** is 2.1.330.

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
