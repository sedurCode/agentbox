# agentbox — constrained sandboxes for coding agents on top of Docker Sandboxes (`sbx`)

agentbox runs coding agents in constrained Docker Sandboxes, with profiles, scoped credentials and a bridge to allow-listed host tools.

- **Requirements:** macOS or Linux, Docker Sandboxes (`sbx`), and Python 3.11+ (standard library only).
- **Status:** early. The `sbx` kit schema is experimental and may change, and so may agentbox's own commands.

`sbx` already does the heavy lifting: each sandbox is a **microVM** with its own kernel,
its own Docker engine, and all egress forced through a **host-side proxy** that enforces
an allow-list and injects API credentials into HTTP headers so the raw keys never enter
the VM. agentbox adds the parts you asked for on top:

- **Profiles** in one config file: agent, workspaces (rw / `:ro`), kits, network allow/deny,
  env, credentials, host tools. `agentbox up h750` → fresh sandbox; `agentbox down h750` → gone.
- **Sandbox-scoped credentials** (never `-g` global) pulled from the keychain, 1Password
  (`op://…`) or a command, removed again on `down`.
- **SSH agent not forwarded** (sbx forwards it by default if `SSH_AUTH_SOCK` is set).
- **Host-tool bridge**: a config-declared allow-list of host commands (flash, probe, gdb…)
  the agent can call with typed, validated parameters against code in its workspace.
- **Audit**: hashes build files, git hooks/config, IDE task files and agent config at
  creation, and shows what changed — those are the files that turn "agent edited my repo"
  into "agent ran code on my Mac".
- **Headless jobs**: `run` starts the agent inside the VM, detached, with a keep-alive so sbx doesn't
  stop the sandbox under it; `wait`, `logs` and `resume` follow it.
- **Progress**: `progress` shows what agentbox measures (time, tool calls, tokens, cost, commits) next to
  the agent's own plan, and flags a job that has gone quiet.
- **Clone mode and `fetch`**: the agent works on its own clone, and `agentbox fetch` brings the branch its
  last job ended on to a review branch on the host. It never merges.
- **`review`**: a pull-request page in the terminal for those branches (summary, commits, files, diff), where
  you fetch, fast-forward merge and clean up. It is your tool: agents never run it.
- **Skill sets**: read-only git repos of agent skills, pinned to a commit you reviewed and linked into the VM.
- **A skill for agents on the host**: `agentbox skill setup` teaches Claude Code, Codex, OpenCode and Pi on
  your machine to drive agentbox, under your ground rules (see *Agents on the host*).
- **`provision`**: guided setup of a new profile, with suggestions taken from the workspace.
- **VS Code** Remote-SSH into the sandbox, so terminals, tasks and extensions run in the VM.
- **Junie** kit (not built into sbx) and an **Arm embedded toolchain** kit.

```
 host (macOS/Linux)                                 microVM  ab-h750
 ───────────────                                ─────────────────────────────
 ~/.config/agentbox/agentbox.toml  (agent can't see)
 sbx daemon ── proxy ── allow-list + key injection ◄── all HTTP(S) egress
 agentbox bridge 127.0.0.1:47111 ◄──────────────── hosttool flash-elf elf=build/fw.elf
   └─ STM32_Programmer_CLI / st-info / arm-none-eabi-gdb  (USB probe lives here)
 ~/src/h750-firmware  ◄═══ direct mount (rw) ═══►  same path inside the VM
 ~/src/stm32-common   ◄═══ mount :ro ══════════►   same path, read-only
```

## Quick start

```bash
brew install --cask docker/tap/sbx && sbx login      # once; Linux and Windows: see Install
brew install sedurcode/agentbox/agentbox             # or the one-line installer; see Install
agentbox setup                                        # once, and after each upgrade
agentbox provision myproj --workspace ~/src/myproj    # guided; clone mode is suggested
agentbox run myproj --prompt-file task.md --detach    # the brief goes in a file, never on a command line
agentbox progress myproj --watch                      # until the job ends
agentbox review myproj                                # fetch (f), read the summary, commits and diff, merge (m)
                                                      # or by hand: agentbox fetch myproj, then
                                                      # git diff HEAD...review/myproj-fix-login and
                                                      # git merge --ff-only review/myproj-fix-login
agentbox down myproj                                  # when finished; asks for confirmation (--force skips
                                                      # that and deletes the sandbox even while it is in use: for scripts)
```

## 1. Install

agentbox needs Docker Sandboxes (`sbx`, signed in with `sbx login`) and Python 3.11+ (standard library only).

**macOS, Homebrew** (upgrades with `brew upgrade agentbox`, then `agentbox setup`):

```bash
brew install --cask docker/tap/sbx && sbx login
brew install sedurcode/agentbox/agentbox             # brings brew's Python
agentbox setup
```

**macOS or Linux, one line** (on Linux, install sbx from docs.docker.com/sandboxes and Python 3.11+ with your
package manager first):

```bash
curl -fsSL https://raw.githubusercontent.com/sedurCode/agentbox/main/install.sh | bash
```

**Windows** (PowerShell):

```powershell
winget install -h Docker.sbx; sbx login
winget install -h Python.Python.3.13
irm https://raw.githubusercontent.com/sedurCode/agentbox/main/install.ps1 | iex
```

The one-line installers download the newest release (pre-releases too; `AGENTBOX_VERSION=v0.1.1` picks one),
check it against the `SHA256SUMS` attached to the release, refuse it if that fails, and run that release's own
`install.sh` / `install.ps1`. To upgrade, run the line again. To read the script first, download it, or download
a release from the Releases page, unpack it and run `./install.sh` (or `.\install.ps1`) in it.

Host tools run with a fixed `PATH` (Homebrew, `/usr/local/bin`, `/usr/bin`, `/snap/bin`, then `~/.local/bin`
last so it can't shadow system tools; missing directories skipped). Use an absolute path for anything else.

`install.sh` puts the CLI in `~/.local/share/agentbox` (the script as `bin/agentbox.py`, plus a
small `sh` launcher `bin/agentbox`), links `~/.local/bin/agentbox`, and runs `agentbox setup` (below). It also installs the guide for
agents on the host (`share/skill/`) but leaves their skill folders alone: run `agentbox skill setup` for that.
It copies the VS Code extension (`share/vscode/`) and, if it finds Rex, the Rex actions (`share/rex/`), but
installs neither into the editor or Rex: see *Rex and VS Code* below.

The launcher looks for Python at run time (`python3.14`, `3.13`, `3.12`, `3.11`, then `python3`;
the first that is 3.11+ wins), so removing or upgrading one interpreter doesn't break the
install. With none it prints "agentbox needs Python 3.11+ (brew install python)" and exits 1.

### `agentbox setup`

Seeds your config dir, `~/.config/agentbox` (or `$AGENTBOX_CONFIG_DIR`), from the defaults that ship with
agentbox in `share/kits` and `share/config`: `agentbox.toml` (from `agentbox.example.toml`), the kits and
the `gdb/` scripts. It only adds what is missing and never overwrites your files: a kit or script you have
that differs from this release's copy is reported as `kept (this release's copy differs)` and left alone.
An old v2 kit (with a `spec.yaml`) is moved aside first. The dir is made owner-only. It ends with the next
steps (`sbx login`, `agentbox doctor`, the optional `skill setup`, `vscode install` and `rex setup`);
`--json` prints the same as one document.

The installers run it for you. Run it yourself after installing any other way, and after each upgrade, so
new kits arrive. Commands that need a config and find none tell you to run it.

### Rex and VS Code

Both are optional and run on the host; each gives you the dashboard (every profile, its job, interactive
sessions and review branches) and its actions. Neither changes anything until you opt in, and re-running
`install.sh` (or `install.ps1`) after an upgrade refreshes the files both use (run `agentbox vscode install`
again too; Rex loads the new files when it restarts).

**VS Code**: one command, then reload VS Code's windows (Developer: Reload Window):

```bash
agentbox vscode install      # installs or updates the extension; run it again after upgrading agentbox
agentbox vscode status       # current, outdated (agentbox has a newer one) or missing; --json
agentbox vscode uninstall    # removes it
```

There is no build step and node isn't needed: `install` zips `~/.local/share/agentbox/share/vscode` into a
`.vsix` and runs `code --install-extension <it> --force`. The VS Code CLI is found as for `agentbox code`:
`[settings] vscode`, else `code` on `PATH`, else the one in the app bundle (no config is needed). See
*Using agentbox from VS Code*.

**Rex**: the installer copies the actions to `~/.local/share/agentbox/share/rex/` when it finds Rex
(`$AGENTBOX_REX`, `rex` on `PATH`, Rex.app or `~/.config/rex/`); then, once:

```bash
agentbox rex setup           # adds the agentbox block to ~/.config/rex/init.lua (creating it if needed)
agentbox rex status          # current, outdated, missing or manual; --json
agentbox rex remove          # deletes that block, nothing else
```

and restart Rex (or reload its config). `setup` appends one block between `-- agentbox: added by ...` and
`-- end agentbox` comments, and touches nothing else in the file; run it again after moving agentbox and it
rewrites the block in place. If `init.lua` already loads agentbox with lines you wrote yourself (the old
install instructions), `status` says `manual` and `setup` and `remove` leave them alone: delete them and run
`setup` to hand them over. Neither the installer nor any other command edits Rex's config. The block is:

```lua
package.path = os.getenv("HOME") .. "/.local/share/agentbox/share/rex/?.lua;" .. package.path
require("agentbox").setup{}
```

Installed Rex after agentbox? Run the installer again. See *Using agentbox from Rex* and
[`share/rex/README.md`](share/rex/README.md).

### One-time sbx settings

1. **Global network policy.** The first `sbx run` asks you to pick one. Choose
   **Locked Down** (deny-all) if you want the profile allow-lists to be the only egress,
   or **Balanced** for common dev hosts allowed globally. Either way, private IP ranges,
   your LAN and host `localhost` are blocked unless explicitly allowed.
   Check with `sbx policy ls`; watch blocks with `sbx policy log`.
2. Don't add global allows (`sbx policy allow network …`) for project needs — put them in
   the profile's `allow`, which lands in a sandbox-scoped kit policy.
3. Optional: `export SBX_NO_TELEMETRY=1`.
4. If you ever started the sbx daemon from a shell with `SSH_AUTH_SOCK` set, restart it
   without: `env -u SSH_AUTH_SOCK sbx daemon stop && env -u SSH_AUTH_SOCK sbx daemon start`.
   `agentbox doctor` runs `ssh-add -l` inside the sandbox and fails if a live agent answers
   (sbx always creates `/run/ssh-agent.sock`, so the socket existing proves nothing). It prints
   only the verdict, never key fingerprints.

## 2. Configure

### New profiles

`agentbox provision <name>` adds a profile for you. It asks for each value you didn't pass as a
flag, suggests settings from the workspace with the reason for each (it only reads files; it
never runs anything from the workspace, git included), shows the TOML it will add and asks
before writing.

```text
$ agentbox provision webapp
Workspace [/Users/me/gits/webapp]:
Agent (claude|copilot|codex|gemini|shell|<kit>) [claude]:

WARNING - files here that an agent in the sandbox loads or could change (not blocking):
  /Users/me/gits/webapp: CLAUDE.md: the agent loads it as instructions and can edit it

Host tools (flashing, debugging on your Mac) need mode = direct; otherwise clone is safer.
Will this profile use host tools? [y/N]:
Mode (clone|direct) [clone]:

Kits:
  suggest python-node: pyproject.toml found
  suggest claude-team: model routing and subagents for Claude profiles
Kits (Enter = python-node, claude-team; 'none'; or comma-separated):

Network (extra hosts the sandbox may reach):
  suggest pypi.org: Python packages (pyproject.toml)
  suggest files.pythonhosted.org: Python package downloads
Allow (Enter = pypi.org, files.pythonhosted.org; 'none'; or comma-separated):
...
This profile will be added to /Users/me/.config/agentbox/agentbox.toml:

[profiles.webapp]
agent = "claude"
workspace = "/Users/me/gits/webapp"
mode = "clone"
kits = ["python-node", "claude-team"]
allow = ["pypi.org", "files.pythonhosted.org"]

Add this profile? [y/N]: y
Added profile 'webapp' (backup of the old config: ...agentbox.toml.bak-20260101T120000Z)
Checks (agentbox doctor webapp): ...
Next:
  agentbox up webapp
  agentbox run webapp --prompt-file task.md --detach
```

Flags: `--workspace`, `--agent`, `--mode clone|direct`, `--kit`, `--skills`, `--allow`,
`--extra PATH[:ro]` (repeatable), `--instructions TEXT`. `--yes` takes every default and never
prompts (without a terminal you must pass it); `--dry-run` prints the block and changes
nothing; `--up` starts the sandbox afterwards.

Your config is never rewritten: it is copied to `agentbox.toml.bak-<UTC time>` (0600), the block
is appended after a blank line (your comments stay), and the whole file is parsed again. If that
fails the backup is restored. `host_tools` are not set up here; add them by hand (§5) and don't
reuse another profile's port. A `clone` default is suggested; `direct` means the agent edits your
host tree and `.git/`, so run `agentbox audit` after sessions.

### Profile keys

Edit `~/.config/agentbox/agentbox.toml`. The example has an STM32H750 profile per agent;
the important keys:

| key | meaning |
|---|---|
| `enabled` | `true` (default). `false` marks a template: `doctor` and `status` print "skipped (disabled)", `up`/`code`/`fresh` refuse it, `down`/`stop`/`tools`/`audit` still work |
| `agent` | `claude`, `copilot`, `codex`, `gemini`, `opencode`, `shell` (built-in) or a sandbox kit name/path (`junie`) |
| `workspace` | primary project, mounted read-write at the same absolute path |
| `extra_workspaces` | more mounts, append `:ro` for read-only |
| `mode` | `direct` (default) or `clone` (agent gets its own git clone, host tree untouched; you `agentbox fetch <profile>` to review). Host tools need `direct`. |
| `kits` | kit names under `~/.config/agentbox/kits/` or paths |
| `skills` | names of `[skillsets.*]` (a git repo of agent skills, mounted read-only, skills picked by path and linked into `~/.claude/skills`), merged with `[defaults]` — see *Skill sets* below |
| `allow` / `deny` | sandbox-scoped egress rules (merged with `[defaults]`; deny wins) |
| `env` | plain env vars for the VM (not for secrets) |
| `secrets` | sandbox-scoped credentials, see §4 |
| `keepalive` | `true` (default). `false`: `agentbox run` doesn't hold an exec session open while a job runs, so sbx may stop the sandbox under a detached job (see *Headless jobs*) |
| `permission_mode` | Claude only (also in `[defaults]`, where it applies to Claude profiles only): `"auto"` (default), Claude Code's auto mode, or `"bypass"`, the old `--dangerously-skip-permissions` — see *Permission mode* in §7 |
| `stall_after` | `300` (seconds). A running job with no event for longer is shown as STALLED by `progress` and `status` (see *Watching a job*) |
| `host_tools`, `host_tools_port` | which `[tools.*]` this sandbox may call, and a unique port per profile |
| `instructions` | extra text appended to the agent's instruction file inside the VM |
| `local_model` | `opencode` only: a model served by Ollama on the host, `{ provider = "ollama", model = "...", port = 11434 }` — see *Local models with opencode* |

Kits shipped in `share/kits/` and seeded into `~/.config/agentbox/kits/` by `agentbox setup` (existing ones are kept; an old
v2 copy — any `spec.yaml`, which sbx reads before a v3 descriptor — is moved aside to
`<kit>.v2-backup-<time>` and replaced):

| kit | what it does |
|---|---|
| `embedded-arm` | arm-none-eabi GCC/newlib, CMake, Ninja, gdb-multiarch, cppcheck, clang-format |
| `junie` | JetBrains Junie CLI as an agent (`agent = "junie"`). The one kit with a recipe (`junie.dockerfile`): sbx builds it on first use, with Docker on the host if it is running, otherwise in its `sbx-kit-builder` sandbox |
| `claude-team` | Claude model routing, **Claude profiles only**: `ANTHROPIC_MODEL=opus`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (no telemetry/error reporting; blocked by the allow-list anyway), plus user-level subagents `reader` (Haiku, read-only lookup), `worker` (Sonnet, scoped edits and tests) and `architect` (Opus, read-only design/review), with instructions to use subagents only when you ask for them. Use as `kits = [..., "claude-team"]` |
| `python-node` | Python 3 with venv/pip, Node and git, plus the Ubuntu/Debian package hosts needed to install them |

Kits use **kit spec v3**: a directory `<kit>/` holding `<kit>.yaml` (first line
`# syntax=docker/sandbox-kit:3`, `schemaVersion: "3"`, typed `capabilities` such as
`network-policy@1`, `lifecycle@1` and `agent-context@2`) and, for a kit with content, `<kit>.dockerfile`.
sbx now ships its own agents (`claude`, `codex`, …) as v3 kits and refuses to mix v2 (`spec.yaml`) kits
into them, so agentbox refuses a v2 kit with a hint instead. To convert your own, see the
[kit spec](https://github.com/docker/sandbox-kit-spec) and its `migrate-kit-to-v3` skill; the kits here
are worked examples. Kit instructions reach the agent progressively: sbx lists each kit in the agent's
`CLAUDE.md`/`AGENTS.md` and keeps the text in a per-kit file the agent reads.

**Keep the config and kits out of every workspace.** A kit in the repo is agent-writable,
so an agent could widen its own allow-list or add host tools for next time. The bridge
refuses to start if the config or kits dir sits inside a mounted workspace.

Then:

```bash
agentbox doctor            # loads kits, checks host tools, paths
```

### Skill sets

A skill set is **one git repo on the host** — typically your fork of a skills repo, e.g. a fork of
`mattpocock/skills` at `~/gits/skills`, laid out as `skills/<bucket>/<skill>/SKILL.md`. agentbox mounts the
whole work tree **read-only** into the sandboxes that want it, and a profile picks the skills it wants **by
path**. Nothing is copied or vendored: the repo is the single source, and git is how you review and update it.

```toml
[skillsets.pocock]
path = "~/gits/skills"                      # a git work tree; mounted read-only as a whole
ref = "a496f5513d4a4cf889bc48e189e579f4b0bdf6aa"   # optional pin: refuse to mount unless HEAD is exactly this
include = ["skills/engineering/tdd", "skills/productivity/grilling", "skills/local/sandbox"]
allow_scripts = ["skills/engineering/diagnosing-bugs"]   # true | false | list of included paths (default false)
description = "my fork of mattpocock/skills"

[defaults]
skills = ["pocock"]              # merged with the profile's own list, like allow / deny / kits

[profiles.voicedev]
skills = ["pocock"]
```

- `include` is a list of relative paths, each a folder holding a `SKILL.md`. The skill's name is the folder's
  last path component (`skills/engineering/tdd` → `tdd`). When `include` is present, **only** those folders are
  skills, and only they are checked. Without `include`, every immediate subfolder is a skill, so a flat folder
  of skills still works.
- `allow_scripts` is `true`, `false` (default) or a list of included paths whose folders may hold non-text
  files and executables. A path in the list that is not in `include` is an error.
- `ref` is the commit you have reviewed (full 40-hex hash). **Pinning is recommended.**

**Files outside the included folders are not checked.** The repo's own `AGENTS.md -> CLAUDE.md` symlink,
`scripts/*.sh`, `package.json`, `.github/`, `.claude-plugin/` and every skill you did not include are mounted
(the mount is the whole work tree) and the agent can read them as text, but they are never linked as skills and
never fail the set. Do not keep anything secret in the repo.

**Rules.** They are checked when the config is loaded for a command that mounts (`up`, `fresh`, `code`,
`shell`, `run`, `resume`), by `doctor` (as `FAIL`s) and again right before every mount. A skill is an
instruction the agent follows, so each rule closes a way to smuggle instructions in or to widen what the
agent controls:

| rule | why |
|---|---|
| `path` exists, is a directory and is not a symlink | what you reviewed is what gets mounted; a link could be re-pointed |
| its resolved path overlaps **nothing the agent can write on the host** (either direction): a `mode = "direct"` workspace, or an `extra_workspaces` entry without `:ro` in either mode. A clone-mode workspace (the agent works on its own clone; the source is mounted read-only) and `:ro` extras don't count. It is also not inside the config dir, does not contain the config file, the kits dir or the state dir, and has nothing to do with `~/.claude` | an agent-writable skill set would let one sandbox plant instructions for every other one; the config/kits/state hold your permissions; `~/.claude` holds your own settings and credentials |
| each `include` path is relative, has no `..`, goes through no symlink at any level (every component is `lstat`ed) and resolves inside the set; two paths may not end in the same name | the include list can't reach outside the repo, and a skill name is unambiguous |
| each skill has a `SKILL.md`, a name matching `^[a-z0-9][a-z0-9-]{0,63}$`, and a name that is neither a Claude Code built-in (`code-review`, `security-review`, `simplify`, `init`, `claude-api`, `run`, `loop`, `schedule`, `update-config`, `keybindings-help`, `fewer-permission-prompts`, `statusline-setup`) nor present in another set of the same profile | a skill must not shadow a built-in or silently replace another set's skill |
| no symlinks inside a skill folder | a link can point at anything the VM can reach |
| text only unless `allow_scripts` covers the skill: every file is `.md`, `.txt`, `.json`, `.yaml`/`.yml` or `LICENSE*`, none is executable, and there are no FIFOs or devices (those are refused even when scripts are allowed) | skills that can't carry code can only persuade, not run |
| `ref`, if set, equals the work tree's HEAD | the mount is live: this is what keeps a different checkout from reaching the agent |

(Without `include`, the immediate subfolders are checked the same way; dot entries such as `.git` and plain
top-level files such as `LICENSE*` or a README are ignored, and a symlink at the top level is refused.)

### Pinning, drift and updates

The mount is **live**. If you check out another branch in `~/gits/skills` — a review branch, say — every
running sandbox sees that content immediately. That is what `ref` is for. HEAD is read as text from `.git`
(agentbox never runs git on the host).

- **With `ref`:** if HEAD is not exactly that commit, `up`, `fresh`, `run` and the other mounting commands
  refuse, and `doctor` reports a `FAIL`, printing both commits. Nothing is created or linked.
- **Without `ref`:** each set's HEAD commit and branch are recorded in the profile's state when the sandbox is
  created. On `up`, `run` and `doctor`, if the commit or the checked-out branch has changed since, agentbox
  prints a `WARNING` that running sandboxes already see the new content, with both commits and the review
  command `git -C <path> diff <old> <new> -- <included paths>`. `agentbox fresh` records the new state.

`doctor` and `status --json` show per set: path, `ref` (if any), HEAD commit, branch, the included skills and
the drift state (`pinned`, `mismatch`, `unchanged`, `changed`, `unrecorded`).

**Updating** is a git flow in the skills repo:

```bash
git -C ~/gits/skills fetch upstream
git -C ~/gits/skills diff main upstream/main -- skills/engineering/tdd skills/productivity/grilling skills/local/sandbox
git -C ~/gits/skills merge upstream/main       # after reading the diff
git -C ~/gits/skills rev-parse HEAD            # put this in `ref`
```

Edit `ref` in `agentbox.toml`. `agentbox fresh` is **not** needed — the mount is live and `ref`, `include`
and `allow_scripts` are not part of the creation-time kit digest (a changed `path` is, and needs one) — but
until you update `ref` the check refuses to create or link, so a merge you haven't acknowledged cannot reach a
new sandbox. Commands that run `up` link new `include` entries on the next run.

None of this makes a skill's text trustworthy: read the diff before you update `ref`.

**How linking works.** Each set is added to `sbx create` as `<path>:ro`, which mounts it read-only at the
*same absolute path* as on the host (direct and clone mode). Then one idempotent `sh` script runs in the VM
and makes `~/.claude/skills/<skill>` a symlink to `<set path>/<included path>`. It runs after create and on
every `up`, `run`, `code`, `shell` and `resume`, but only against a **running** sandbox — it never boots one.
When the sandbox is stopped (typically the first `up`), a background thread waits until `sbx run` or
`sbx exec` has started it and then links, logging to `<state>/skills.out` instead of your terminal; start
the agent again if it had already read its skill directory.

- Links agentbox made are listed in `~/.claude/skills/.agentbox-links`. It replaces only those, and
  removes those whose skill is no longer configured. Real folders and other symlinks — skills the agent
  writes itself — are never touched. That file is agent-writable, so a name from it is only used if it is
  a plain skill name *and* the entry is a symlink.
- A **new mount** (a new set or a changed `path`) needs `agentbox fresh`, and the message says so when the
  paths changed. A skill whose folder isn't mounted yet is reported as "not linked", not linked dangling.
- The generated kit's instructions name the mounted sets and tell the agent they are read-only.

### Local models with opencode

A profile can run [opencode](https://opencode.ai) in the sandbox on a model that
[Ollama](https://ollama.com) serves on your Mac, so nothing leaves the machine. Claude profiles are
unaffected.

```toml
[profiles.webapp-local]
agent = "opencode"
workspace = "~/gits/webapp"
mode = "clone"
local_model = { provider = "ollama", model = "gemma4:12b", port = 11434 }
stall_after = 900      # local models can think for minutes without an event (see *Watching a job*)
```

`agentbox provision webapp-local --agent opencode` asks for this and suggests the models in your `ollama list`
that report tool support (`--local-model MODEL`, `--local-model-port PORT` without prompts).

- **Keys.** `provider` is `ollama` (the only one so far). `model` is the Ollama name, letters, digits and
  `. _ - / :` only. `port` defaults to `11434` and must differ from every profile's `host_tools_port`.
  `context` (default `32768`) is the context window opencode assumes; set it to what Ollama really gives the
  model. Any other agent with `local_model` is refused.
- **What it sets up**, in the generated kit (the state dir, not the workspace): `localhost:<port>` on the
  sandbox's allow-list, and an opencode config at `/home/agent/.agentbox/opencode.json` (`OPENCODE_CONFIG`
  points at it) with an OpenAI-compatible provider at `http://host.docker.internal:<port>/v1` and this model as
  the default. `OPENCODE_DISABLE_MODELS_FETCH` and `OPENCODE_DISABLE_AUTOUPDATE` are set, since neither
  models.dev nor npm is on the allow-list. Headless jobs also pass `--model=ollama/<model>`, so an
  `opencode.json` in the repo can't switch them to another provider. Changing `local_model` needs
  `agentbox fresh`.
- **The trade-off.** sbx's global policy keeps the host's localhost closed. With `local_model` the sandbox can
  reach exactly one host port, the model server's, and the agent can send it anything Ollama's API accepts:
  prompts, but also `pull`, `delete` or `create` of models (Ollama has no authentication). Only use it with a
  server you are happy to expose to the agent like that, and don't run anything else on that port.
- **Context length.** Ollama decides how much context a model gets: `OLLAMA_CONTEXT_LENGTH` in the environment
  of `ollama serve` (in the Mac app, Settings > Context length) or `PARAMETER num_ctx` in a Modelfile. Past it,
  prompts are cut silently, and an agent's system prompt and tool definitions alone take thousands of tokens.
  Use at least 32k. `agentbox doctor <profile>` checks from the host that Ollama answers on the port, has the
  model and reports the `tools` capability, and warns when the model's window, or the one Ollama runs it with
  (visible while it is loaded), is under 32k.
- **Expectations.** A model that fits on a laptop is a much weaker agent than Claude: it loses track of long
  tasks, calls tools wrongly and claims success it didn't reach. Give it small, well-specified tasks with tests,
  and check the measured numbers and the commits, not its summary.

## 3. Daily use

```bash
agentbox up h750           # first time: create + scoped secrets + attach. Later: re-attach
agentbox up h750 -- --resume   # extra args go to the agent
agentbox shell h750        # bash inside the VM
agentbox tools h750        # what the agent can run on the host
agentbox audit h750        # what changed in hooks/build/IDE/agent config since creation (direct mode)
agentbox fetch h750        # clone mode: the job's branch, into review/h750-<branch> (see §6)
agentbox review h750       # clone mode: review those branches like a pull request, and merge (see §6)
agentbox history h750      # the jobs it ran: when, exit, cost, branch, the brief's title (--json)
agentbox stop h750         # pause sandbox + bridge
agentbox down h750         # audit, delete sandbox, scoped secrets, local state (--force: also kills a running job, skips sbx's
                            # confirmation - required without a TTY - and deletes the sandbox even if in use, e.g. an open VS Code session)
agentbox fresh h750        # down + up — use after editing the profile (kits apply at creation); keeps Claude's sessions
agentbox template h750     # prebuild the profile's kits into an sbx template: new sandboxes start in seconds
agentbox status
```

`down` and `fresh` delete the VM, so in **clone mode** they refuse while it holds work the host does not
have: a branch whose commits the host repo lacks, uncommitted changes or stashes. Fetch it first
(`agentbox fetch <profile> --start`), or pass `--force` to drop it. `fresh` also copies Claude's session
history (`~/.claude/projects`) out of the old VM and into the new one, so `claude --resume` still finds
your conversations; `down` deletes them with everything else. (sbx does not keep kit volumes across
`sbx rm` yet, so agentbox copies them itself.)

A new sandbox takes a little while on first create (kit installs run); re-attaching is quick.

**Prebuilt templates.** For a profile with slow kits (apt toolchains such as `embedded-arm`, or your own
C++/JUCE kit), `agentbox template <profile>` builds a throwaway sandbox from the profile's agent and kits
only — an empty workspace, no host tools, secrets or generated kit — and saves it with
`sbx template save` as `agentbox-<profile>:<time>`. From then on `up`/`fresh` create that profile's
sandboxes from it (`--template … --pull never`), and the kits' install steps see their packages already
installed and skip. In one measurement a C++/JUCE profile went from 64 s to 6 s per sandbox. The template is
tied to a fingerprint of the agent and the kits' files: change a kit and agentbox stops using it and says so.
It also freezes the agent version (and the packages) until you rebuild, so rebuild now and then.
`agentbox template <profile> --remove` deletes it. A `template` set in the profile wins over a prebuilt one.
Templates are local to the machine (sbx's image store).
For throw-away work: `agentbox fresh <profile>` every time.

Inside the sandbox agents run in their "skip permissions" mode — the VM is the boundary.
Use `Shift+Tab` / `/permissions` in Claude Code if you want prompts back.

### Headless jobs

For scripted or long unattended runs (VoiceDev's gateway, CI-like use) `agentbox up -- -p "..."` is
fragile: it needs an attached terminal, so the `sbx run` client dies on daemon timeouts and the
output is lost. `agentbox run` starts the agent *inside* the VM, detached from any session, and
keeps its state in files you can poll. Supported agents: `claude` and `opencode`.

```bash
agentbox run h750 --prompt-file task.md --detach     # prints the job id, returns at once
agentbox wait h750 --timeout 3600                    # block; exit status = the job's exit status
agentbox logs h750 -f                                # follow, one human line per event
agentbox logs h750 --raw                             # the raw stream-json lines
agentbox resume h750                                 # interactive `claude --resume=<session id>` (opencode: --session=)
agentbox progress h750 [--watch]                     # how far along it is (see *Watching a job*)
agentbox run h750 --prompt-file task.md              # without --detach: run, then follow logs, then exit with the job's status
agentbox run h750 --prompt-file - --agent-args --model opus < task.md
```

- **Prompt handling.** The prompt is sent into the VM on the `sbx exec` *stdin* and stored in
  `prompt.md` (mode `0600`); the agent reads it on stdin. agentbox never puts it on an `sbx` or VM
  command line. `--prompt-file` (or `-` for stdin) is preferred over `--prompt TEXT`, because
  `--prompt` text is part of *your own* `agentbox` command line on the Mac. `--agent-args ...`
  takes the rest of the line; the profile's `agent_args` (meant for interactive `up`) are not used.
- **What runs.** `claude -p --output-format stream-json --verbose --permission-mode auto` (Claude Code's
  auto mode; with `permission_mode = "bypass"` it is `--dangerously-skip-permissions` instead), or
  `opencode run --format json --auto` (plus `--model=ollama/<model>` with `local_model`); the VM is the
  boundary, as for `up`, and auto mode is a second layer inside it (see *Permission mode* in §7). In auto
  mode `run` waits up to 30 s for Claude's first event: if Claude Code started in another mode (auto mode
  unavailable for the plan, model or version) it stops the job and fails, suggesting `bypass`. It runs in the primary workspace, via `setsid nohup`, so the job survives the
  exec session that started it. Both read the prompt on stdin (`opencode run` does when stdin is not a
  terminal; checked with opencode 1.19.6), so it is on no command line.
- **Job directory** (inside the VM, `~/.agentbox/jobs/<id>/`, id like `20260101T120000Z-ab12cd`):
  `prompt.md`, `wrap.sh` (the wrapper), `jobprogress` and `progress.tsv` (see *Watching a job*), `start_head`, `pid`, `started_at`, `output.jsonl` (the stream),
  `stderr.log`, `branch` and `end_head` (the branch and commit the workspace ended on; see *Which branch*),
  `finished_at`, `exit_code` (written last, atomically, by the wrapper) and
  `session_id` (the stream's first `session_id`, or `sessionID` for opencode). The host keeps only
  `~/.local/state/agentbox/ab-<profile>/job.json` (current job id, session id, and the result once seen).
- **Job state** comes from those files only: `finished` if `exit_code` exists; `running` if the
  `pid` file's process is alive **and** `/proc/<pid>/cmdline` runs this job's `wrap.sh` (so a
  reused pid doesn't count); otherwise `lost`. Processes are never found with `pgrep -f`, which
  matches its own command line. A **VM reboot, `stop`, or `down` loses a running job**: its state
  becomes `lost` (the files may remain after a reboot; the process doesn't).
- **Session ids** are validated (`[A-Za-z0-9][A-Za-z0-9_-]{7,79}`, so never an option-looking `-...`) wherever they
  are read from the VM, and passed as the single argument `--resume=<id>` (opencode: `--session=<id>`).
- **Why `resume`, not `--continue`:** `claude --continue` doesn't find a `-p` session; `--resume <id>`
  does, and agentbox records the id. `resume` refuses while the job is still running, and for agents
  without a resume flag.
- **Keep-alive (on by default).** `sbx` **does** stop idle sandboxes: one with no open session is stopped
  about 30 s after the last one ended, killing whatever runs in it. Seen on a real job: it started at
  10:46:30Z, which is also the sandbox's `last_used_at` in `sbx ls --json` (the end of agentbox's last
  `sbx exec`); its output stopped at 10:46:58Z with the job still running, and the sandbox was then
  "stopped". (`sbx settings ls` has no setting for it.) So `agentbox run` starts a host-side
  `agentbox keepalive <profile>` process (pid in the state dir) that holds an `sbx exec` session open in
  the VM and re-opens it if it drops; that session is what keeps a detached job alive. It first checks
  `sbx ls --json` and exits if the sandbox isn't running (its own `exec` would otherwise boot it), ends
  when the job does, and `stop`/`down` stop it. Opt out with `run --no-keepalive` or `keepalive = false`
  in the profile (then the job survives only while something else keeps a session open). To see the idle
  clock, look at `last_used_at` in `sbx ls --json`.
- **Stopped sandboxes are never booted by accident.** `sbx exec` starts a stopped sandbox, so agentbox
  first asks `sbx ls --json`. If the sandbox isn't running, `status`, `wait`, the keep-alive and `audit`'s
  `git status` treat the job as `lost` (cached in `job.json`, so later calls make no `sbx` call at all) and
  `doctor` skips its in-sandbox checks ("skipped (sandbox stopped)"; `doctor --start` boots it first).
  `logs` exits 3 instead of booting; `logs --start` boots it to read the output. `down` and `fresh` do boot
  a stopped sandbox, just before deleting it: to check a clone for unfetched work and (`fresh`) to copy
  Claude's sessions out.
- **Retries.** `run`, `wait` and `logs` retry transient `sbx` errors (`context deadline exceeded`,
  connection reset/refused) with exponential backoff (1, 2, 4, 8 s; five attempts). A failed
  `create` is not repeated blindly: agentbox first checks whether the sandbox now exists.
- **`down` / `fresh`** refuse while a job is running; `--force` kills it (the wrapper's process
  group, or its children and then the wrapper if there is no `setsid`, after checking the pid is really this job's wrapper) and removes the sandbox anyway.

Exit codes for `wait` (and the final status of a foreground `run`): the **job's own exit code**
(claude and opencode: `0` success, `1` error) when it finished; `4` = `--timeout` expired with the job still
running; `5` = job lost. Codes 1-3 keep their meaning from the table above for agentbox's own
errors, so a job that exits with 2-5 is ambiguous: read `status --json` (`job.exit_code`) if you need to tell.

### Handing a task to an agent

The whole loop, for a clone-mode profile (`provision` suggests one):

1. **Write the brief** in a file, for example `task.md`: what to do, where the tests are, what "done"
   means, and what not to touch. It reaches the VM on stdin: use `--prompt-file task.md`, or
   `--prompt-file -` with a pipe. Avoid `--prompt TEXT`, which sits in your own process list.
2. **Start it**: `agentbox run h750 --prompt-file task.md --detach` prints the job id and returns. Add
   `--expect-commits N` if you know how many commits to expect. The first `run` creates the sandbox.
3. **Watch it**: `agentbox progress h750 --watch` for the plan and the measured numbers, or
   `agentbox logs h750 -f` for what it is doing. `status` lists every profile.
4. **Wait for it** in a script with `agentbox wait h750 --timeout 3600`; the exit status is the job's own
   (`4` = still running, `5` = lost).
5. **Fetch**: `agentbox fetch h750` holds the sandbox up, waits for the git remote, fetches the branch the
   job ended on (say `fix-uart`) into `review/h750-fix-uart` and prints the new commits and a diffstat. It
   says which ref it chose and why; with no branch recorded it fetches `main` into `review/h750`. If the
   job is still running it warns you, because what you got is not its final state.
6. **Review** on the host: `git log HEAD..review/h750-fix-uart` and `git diff HEAD...review/h750-fix-uart`.
   Nothing has been merged, and your checked-out branch is untouched.
7. **Merge** when happy: `git merge --ff-only review/h750-fix-uart`. If HEAD has moved on and a fast-forward isn't
   possible, rebase the review branch or merge by hand. To ask for changes, run again in the same sandbox
   (`agentbox resume h750` continues the session interactively).
8. **Finish**: `agentbox down h750` removes the sandbox, its scoped secrets and its state. Fetch first:
   the commits live only in the VM until you do. To start clean with the same profile, use
   `agentbox fresh h750`; to carry on, just `run` again.

**sbx stops idle sandboxes**, about 30 s after the last `exec` or `run` session ends, and removes the
`sandbox-ab-<profile>` git remote with it. A running job is protected by the keep-alive, and `fetch` opens a
session of its own while it works. Once the job has ended and nothing is attached, the sandbox soon stops.
`agentbox fetch h750 --start` boots it again, but booting relaunches the agent that was last started with
`sbx run`, which is why it is not the default.

**If the sandbox is gone**, `fetch` says so and exits 1. Commits fetched before it was removed stay in the
host repo under `refs/sandboxes/<sandbox>/<branch>`; keep one with
`git branch LOCAL refs/sandboxes/ab-h750/main`, and `fetch` prints that command when such refs exist.

**Which branch.** When a job ends, its wrapper records the branch the workspace is on (`git symbolic-ref
--short -q HEAD`, nothing for a detached HEAD) and the commit at HEAD. Both are agent-controlled, so the host
takes the branch only if it is a plain name: at most 100 characters of `A-Z a-z 0-9 . _ / -`, no leading
`-` or `.`, no `..`, `//`, trailing `/` or `.lock`, not `HEAD` or `refs/...`, and a valid git branch name.
Anything else counts as unknown. The commit must be 40 hex characters. They show as `branch` and `end_head`
in `status --json` (`job.*` and `job.progress.*`) and `progress --json`, on a `branch` line in `progress`,
and in the dashboard's details; `null` while the job runs or when unknown. `fetch` then defaults `--ref` to
that branch and `--branch` to `review/<profile>-<ref>` (each `/` becomes `-`: `review/<profile>/<x>` can't
coexist with a `review/<profile>` branch in git). With no branch recorded it is `main` into
`review/<profile>`, as before. An explicit `--ref` or `--branch` always wins (`--ref` alone still goes into
`review/<profile>`), and `--force` is never implied.

### Watching a job

```bash
agentbox progress h750                      # a few lines, once
agentbox progress h750 --watch              # redraw until the job ends; exit status = the job's, like `wait`
agentbox progress h750 --json               # the `job.progress` object of `status --json`
agentbox run h750 --prompt-file task.md --detach --expect-commits 3
```

```
job 20260101T120000Z-ab12cd  running  elapsed 12m04s  last activity 6s ago
plan [########------------] 3/7  now: step 4: wire up the UART  (agent's plan, not verified)
note: the linker script needed a fix  (agent's note)
commits 1/3  turns 88  tool calls 61 (Bash 30, Read 14, Edit 9, Grep 5, Task 2)  cost ~$1.84 (estimate)
tokens in 912  out 41200  cache read 3811000  cache write 210000
```

**Measured** (computed by agentbox, not by the agent):

- *elapsed*, and the age of the last event in `output.jsonl` (from the events' own `timestamp`). A running
  job with no event for more than `stall_after` seconds (profile key, default `300`) is **STALLED**.
- *turns* (assistant messages), *tool calls* (`tool_use` blocks, top five by name) and *tokens*. stream-json
  repeats a message id, and its usage, across content-block events, so every message id counts once.
  opencode's `--format json` stream is read too: a turn is a step (`step_start`, one model call), its tokens
  come with `step_finish` (reasoning tokens count as output), a tool call is a `tool_use` event. Its cost is
  not used: it is 0 for a local model, so `cost_usd` stays `null`. opencode writes an event only when a part
  is finished, so a slow local model writing a long answer looks idle; raise `stall_after` for those profiles.
- *cost*: the exact `total_cost_usd` from the agent's final `result` once the job is over. While it runs,
  an estimate from the token totals and the `PRICES` table in `bin/agentbox` (per model family: opus, sonnet,
  haiku, fable), shown with `~` and "estimate". Those prices **need checking against current pricing**; a model
  that isn't in the table gets no cost rather than a wrong one.
- *commits*: `git rev-list --count <start>..HEAD` in the primary workspace, run inside the VM, where
  `<start>` is the HEAD `run` recorded in the job dir (`start_head`) before launching the agent. With
  `run --expect-commits N` it reads `commits k/N`. No git repo, or an unreadable `start_head`: no number.

**Reported by the agent** (it can be wrong, stale or invented, and is labelled "agent's plan"): `run` puts
one short paragraph at the start of the prompt (on stdin, as the rest) asking the agent to record a plan and
keep it current with `jobprogress`, a small POSIX `sh` script in the job dir whose directory is on the end of
the agent's `PATH` (`AGENTBOX_JOB_DIR` names it):

```
jobprogress plan "write the test" "make it pass" ...   # replace the plan; every step pending
jobprogress start 2                                    # step 2 is in progress
jobprogress done 2                                     # step 2 is completed
jobprogress note "waiting for the flash tool"          # free text; the last one wins, "" clears it
```

Limits: at most 50 steps, 200 bytes per step, 300 per note, no control characters, a step number must be
inside the plan; the command refuses anything else with exit 2. It writes `progress.tsv` in the job dir
through a temp file and `mv`: one `<kind><TAB><text>` line per entry (`pending`, `active`, `done`, or
`note`), so there is no JSON escaping in `sh`; agentbox builds the JSON. Because the agent can write
that file directly (and the VM is its machine), agentbox treats it as untrusted: the VM returns at most 60 lines
and 20 KB of it, the host drops malformed lines, caps steps (50) and text, and runs everything through
`safe_text` (control and bidi characters print as `\x1b`, never raw). An agent can misreport its
plan, finish it early, or skip it; use the measured lines to check. `run --no-progress` leaves out the
prompt paragraph and the reporter. With no plan, `progress` shows only the measured lines.

**How it is read.** `output.jsonl` is not re-read on every `--watch` tick: the host keeps, in the state
dir, the byte offset already processed and the running totals, and each look fetches only newer bytes (whole
lines only, in one `sbx exec` together with `progress.tsv` and the commit count). If the file gets shorter
(the agent can write it) the totals are reset and recounted. When a job ends, its final values are cached in
`job.json`, so `progress` and `status` on a finished job make no `sbx` call. A stopped or missing sandbox is
never started: `progress` shows the cached values and says so ("sandbox stopped: showing cached values").
`wait` and the keep-alive cache the final values as the job ends; if neither saw it (the sandbox stopped
first) the last cached totals are shown.

**`status`.** Each job line gets a suffix, `3/7 · 12m · 6s ago` (plan steps, elapsed, last event) or
`STALLED 7m`. `status --json` adds `job.progress` (`null` when there is no job), from the same single
`sbx ls --json`; a running job costs at most one extra exec, others none:

```json
"job": {"id": "20260101T120000Z-ab12cd", "state": "running", "...": "...",
        "progress": {"elapsed_s": 724, "last_event_age_s": 6, "stalled": false,
                     "turns": 88, "tool_calls": 61,
                     "tools_top": [{"name": "Bash", "count": 30}, {"name": "Read", "count": 14}],
                     "tokens": {"input": 912, "output": 41200, "cache_read": 3811000, "cache_creation": 210000,
                                "total": 4063112},
                     "cost_usd": 1.84, "cost_is_estimate": true,
                     "commits": 1, "expect_commits": 3,
                     "plan": {"steps": [{"text": "write the test", "status": "done"},
                                        {"text": "make it pass", "status": "active"}],
                              "current": 2, "done": 1, "total": 2},
                     "note": "waiting for the flash tool", "stale": false, "denials": 0,
                     "branch": null, "end_head": null}}
```

`plan` is `null` when the agent declared none, `plan.current` is the first `active` step (1-based) or
`null`, a step's `status` is `pending`, `active` or `done`, `elapsed_s` is up to now (running) or up to
`finished_at`, `last_event_age_s` is `null` unless the job is running, and `stale` is true when the values
come from the host's cache because the sandbox could not be read. `denials` counts the actions Claude
Code's permission check refused (see *Permission mode* in §7; always 0 for opencode). `branch` and `end_head` are the branch and
commit the workspace ended on (`null` while the job runs, or when unknown; see *Which branch*).

### Dashboard

```bash
agentbox dashboard                  # one snapshot
agentbox dashboard --watch          # redraw every 5 s until Ctrl-C (--interval S, at least 2)
agentbox dashboard --interactive    # (-i) full-screen: every profile, its job and plan; keys below
agentbox dashboard --json
```

One card per running sandbox: VM uptime, load, CPU, memory and disk bars, and for its job the elapsed time,
idle time (since the agent's last event), turns, tool calls, tokens and cost (an estimate unless the agent
reported one). Stopped and disabled profiles are listed on one line and are never booted. A running sandbox
costs one extra `sbx exec` (CPU is sampled over half a second inside the VM) on top of what `status` does.
The VM's output is parsed as numbers and drawn by agentbox, never printed raw. Colour is used only on a
terminal and not if `NO_COLOR` is set.

**Interactive sessions.** The same exec also looks for agent sessions you opened yourself (`agentbox up`, Rex's
"Open Profile"): every `claude` or `opencode` process (by its executable's name) that isn't the current
headless job or one of its children, how long ago the newest Claude transcript (`~/.claude/projects/*/*.jsonl`)
or opencode session store (`~/.local/share/opencode/`) was written, and the transcript's last
`permissionMode` (one `grep` over its final 64 KB). Only process metadata, file times and that one mode word
are read, never conversation content, and never from a stopped sandbox. A card then gets a line like
`interactive: 3 sessions, active 9s ago, mode auto`. `--json` adds to each row `sessions`
(`[{"agent": "claude", "age_s": 10735}]`, interactive processes only, at most 32), `activity_age_s` (seconds
since the newest write, or `null`) and `permission_mode` (`auto`, `acceptEdits`, `bypassPermissions`, `plan`,
`default`, `dontAsk`, `manual`, or `null`); a VM that isn't running has `[]`, `null`, `null`. Each row also has
`state`, the interactive view's state for it: `working`, `interactive_working`, `stalled`, `error`, `lost`,
`interactive_idle`, `done`, `nojob`, `stopped`, `disabled` or `unknown`.

`--interactive` (`-i`) is a full-screen terminal view (Python's `curses`, nothing to install) and needs a
terminal; it can't be combined with `--watch` or `--json`. A list of every profile, not only the running
ones, sorted: working jobs, then working interactive sessions, then those needing attention (stalled,
failed, lost), then idle interactive sessions, then finished, then the rest. A running headless job decides
a row's state; otherwise, if interactive sessions run there, the row is *interactive* (a blue ◉, `@` in
ASCII) when one wrote in the last 90 seconds and *interactive, idle* (a blue ◦, `i`) when not, whatever an
older job says. Each row has a state mark, the agent, and the job's elapsed time, how long ago an
interactive session last wrote (`9s ago`, `idle 12m`), or how long ago the job finished. Beside it
(or below it in a narrow pane) the selected profile's details: the VM with CPU, memory and disk bars, the
interactive sessions (each agent and how long it has run, the last activity and the permission mode;
`bypassPermissions` in yellow with "(bypass: no permission checks)"), then the job (while sessions run and
no job does, only a dim "last job: done 10d ago" line; otherwise its id, state, start and finish, exit code, elapsed, idle, turns, tool calls, the most-used tools, commits,
tokens, cost with `~` when estimated), small charts of tokens and tool calls per refresh since the dashboard
started (kept in memory only), and the agent's plan, which is the agent's own claim, not verified. Data is
collected in the background every `--interval` seconds, so keys and resizing stay responsive while `sbx` is
slow. It uses the terminal's own colours and background, honours `NO_COLOR`, and draws ASCII if the locale
isn't UTF-8. Below 60x15 it asks for a bigger pane.

| key | |
|---|---|
| ↑ ↓, j k, click | select a profile; the wheel scrolls the list or the details; Home / End |
| Enter, p | `agentbox progress <profile> --watch` in the same terminal; back on exit or Ctrl-C |
| l | `agentbox logs <profile>` in `$PAGER` (default `less -R`), sanitised as `logs` always is |
| f | shows `agentbox fetch <profile>`, the ref it will fetch and the review branch, and asks y/N; runs it only on `y`, with no other flags. A stopped sandbox gets a note instead: fetch needs it running, and booting it would relaunch the agent |
| u | `agentbox up <profile>`: the agent's session takes this terminal, and the dashboard comes back when it exits (sbx keeps the sandbox running). Inside Rex (`REX_SESSION` and `REX_BLOCK` set, and a `rex` CLI found: `$AGENTBOX_REX` if it's an absolute path, else `rex` on `PATH`, else the one in Rex.app) it opens in a new Rex tab instead, with `rex run`; if that fails, the next `u` runs it here. Refused for a disabled profile; allowed, with a warning, while a job runs there (`up` attaches to its sandbox) |
| s | stop a running sandbox: shows `agentbox stop <profile>` and runs it on `y`, with no other flags. If a job is running there, the job is killed and reported as lost, so the profile name must be typed exactly, then Enter; Esc or anything else cancels |
| v | `agentbox review <profile>` for a clone-mode profile: in a new Rex tab inside Rex (as `u`), else in this terminal, back when you quit it. A profile with branches ready to review, or job branches not fetched yet, has a ◆ in the list and "N commits ready to review" in its details (the same statuses `review` shows) |
| r | refresh now |
| ? | help |
| q, Ctrl-C | quit and give the terminal back |

The dashboard runs only `progress --watch`, `logs`, `fetch`, `up`, `stop` and `review`, each with only the profile name;
it never offers `--force`, `--start`, `down`, `fresh`, `run` or `resume`.

### Using agentbox from VS Code

This is VS Code on your Mac watching the sandboxes; for a VS Code window that works inside one, see *VS Code
inside the sandbox* below. `agentbox vscode install` (see *Rex and VS Code* in §1) adds the dashboard to VS Code as an extension; details
in [`share/vscode/README.md`](share/vscode/README.md):

- **Profiles view** (the agentbox icon in the activity bar): every profile in the interactive dashboard's
  order, with its state, timing and agent, and a ◆ when branches wait for review. Expand one for the VM's
  CPU, memory and disk, interactive sessions and their permission mode, the job's stats and plan, and the
  review counts.
- **Actions** on a profile's context menu or the command palette: watch progress, show the job log, fetch
  (asks first), review, open an agent session (`up`), stop (asks first; the profile name must be typed if a
  job runs there), and "Open Workspace in VS Code" (`agentbox code`). Like the dashboard, never `--force`,
  `--start`, `down`, `fresh`, `run` or `resume`.
- **Status bar** summary and **notifications** when a job finishes, fails, stalls or is lost.

It reads `agentbox dashboard --json` every 5 s while the view is visible and every 30 s while it is hidden
(`agentbox.refreshInterval`, `agentbox.backgroundInterval`); `agentbox.path` points at another agentbox.
The rows' `state` field it needs is new: with an older agentbox the view asks you to run the installer again.

### Using agentbox from Rex

[Rex](https://www.superlogical.com/rex/docs) users get palette actions and job status from
`share/rex/` (installed to `~/.local/share/agentbox/share/rex/` when the installer finds Rex; setup in
*Rex and VS Code* in §1 and [`share/rex/README.md`](share/rex/README.md)):

- **Actions** (category "agentbox"): open a profile as a session with Agent, Progress and Shell tabs, run
  a job from a prompt file, open the interactive dashboard, fetch for review, review (`agentbox review` in a
  "Review" window of the profile's session; the picker shows "2 ready · 1 not fetched"), stop a sandbox,
  remove a sandbox. `agentbox rex setup` adds
  them to `~/.config/rex/init.lua` (see *Rex and VS Code* in §1).
- **Status in tabs and View Activity**: inside Rex, `progress --watch`, `dashboard --watch` / `--interactive` and `wait`
  report the job's state with OSC 7501: working (with plan progress), blocked when stalled, done, error,
  idle. Each record is named after its profile; the dashboard keeps one per profile, and a finished job's
  done or error stays after the sandbox stops, until that profile's next job. While interactive sessions run
  in a sandbox with no running job, its dashboard record is `working` ("interactive") or `idle`
  ("interactive, idle 12m"), never done, error or blocked; when they end it follows the latest job again. Only host-side state goes in
  (job state, exit code, profile name, agentbox's own wording), never text from the sandbox. Reports are sent only to a
  terminal with `REX_SESSION` set, never with `--json`; `AGENTBOX_NO_OSC=1` turns them off.
- **Review**: while `agentbox review` runs inside Rex it sets the tab's title to `review: <profile>/<branch>`
  and keeps one `review` record, `idle` with "N to review" while anything waits for you, cleared when nothing
  does or when it quits. It is never `blocked`, `done` or `error`, so it never notifies.
- **Notifications**: Rex notifies by itself while in the background (Settings > Notifications > Program
  Activity), once per tab that reports, so a job watched from two tabs notifies twice.

### VS Code inside the sandbox

```bash
agentbox code h750                 # opens the primary workspace
agentbox code h750 ~/src/stm32-common   # or another mounted workspace
```

This uses sbx's built-in SSH access and the **Remote - SSH** extension
(`ms-vscode-remote.remote-ssh`). It runs `sbx setup ssh` the first time, which adds a managed
`*.sbx` block to `~/.ssh/config`. No SSH server runs in the VM and no port is opened: `ssh`
goes through `sbx ssh proxy` to the daemon over a local socket, with no keys or agent, and
only while you're logged in to Docker. The command creates the sandbox if needed (without
starting the agent), starts the host-tool bridge, checks SSH, then runs
`code --remote ssh-remote+ab-<profile>.sbx <folder>`.

- Terminals, tasks, launch configs, debuggers and extensions in that window all run **inside
  the VM**, so an agent-edited `.vscode/tasks.json` can't touch your Mac. Opening the same
  folder in a normal local window *does* run them on the host — audit first.
- agentbox refuses to connect if your SSH config would forward your agent to `*.sbx`
  (typically `Host *` + `ForwardAgent yes`). Fix with `Host *.sbx` / `ForwardAgent no`
  above the `Host *` block.
- Set `vscode = true` on the profile (then `agentbox fresh`) to allow the hosts VS Code's
  server download and Marketplace use. Alternatively set
  `"remote.SSH.localServerDownload": "always"` in VS Code so the server is fetched on your
  Mac and copied in, and install extensions as VSIX.
- First connection is slow (server install). On macOS reconnect loops, set
  `"remote.SSH.useLocalServer": false`. Host-key errors: delete the
  `docker sandboxes (managed)` block in `~/.ssh/config`, run `sbx setup ssh`.
- VS Code port forwarding works but only reaches loopback inside the VM, and listens on
  `127.0.0.1` on your Mac unless you change it.

### Agents on the host (the agentbox skill)

Agents running on your own machine (Claude Code, Codex, OpenCode, Pi) can drive agentbox for you: propose
a profile, write a brief, start and watch a job, fetch the result and tell you it is ready on
a `review/<profile>-<branch>` branch. agentbox ships a skill that tells them how, and what they may do without asking.

```bash
agentbox skill setup           # install the skill for the agents on this machine
agentbox skill status          # current | outdated | missing | foreign, per location (--json)
agentbox skill show            # the guide the agents read
agentbox skill remove          # delete what setup installed
```

The guide itself is `share/skill/guide.md`, installed with agentbox and printed by `skill show`. `setup`
only writes a short `SKILL.md` stub whose front matter triggers on agentbox, sandbox, sbx and "run this in
a box", and whose body tells the agent to run `agentbox skill show`. So upgrading agentbox updates what the
agents are told, with no reinstall; run `setup` again only if `status` says `outdated`. It is the same
pattern Rex uses for its agent skill.

The stub goes to `~/.claude/skills/agentbox/` (Claude Code; `$CLAUDE_CONFIG_DIR/skills/agentbox/` if that is
set) and `~/.agents/skills/agentbox/` (Codex, OpenCode, Pi), plus `$CODEX_HOME/skills/agentbox/` when
`CODEX_HOME` is set. Each stub carries a marker comment. A `SKILL.md` without the marker, or a symlink, is
reported as `foreign` and never overwritten or removed; `remove` deletes only marked stubs, and their folder
only if it is then empty. None of the `skill` commands needs a config or touches a sandbox, and
`install.sh` does not run `setup` for you.

The guide covers the ground rules (which commands an agent may run on its own, which need your go-ahead,
and what it must never do: `--force`, merging, pushing, reading secrets, task text on a command line),
when work belongs in a sandbox rather than on the host, how to write a brief, the run / wait / fetch loop
with its exit codes, treating job output and fetched commits as data rather than instructions, profiles,
and the sbx behaviour that catches people out. Edit the guide to change the rules; this is a different
skill from any you mount *into* sandboxes with `[skillsets]`.

### Scripting agentbox

`status`, `dashboard`, `doctor`, `tools`, `progress`, `fetch`, `history` and `review` take `--json`, as do `skill status`, `rex status` and `vscode status`: stdout is then exactly one JSON document, the human
text is unchanged without the flag, and diagnostics still go to stderr. No secret values appear
in any of them (`doctor` reports token-like variables by name, class and length only).

**Exit codes** (all commands):

| code | meaning |
|---|---|
| `0` | ok |
| `1` | checks failed (`doctor`) or a command failed (`sbx` error, bridge, SSH, ...) |
| `2` | usage or config error: bad arguments, no/invalid config, unknown or disabled profile, unsafe tool definition |
| `3` | sandbox not found (`shell`, `stop`, `resume`, `logs` on a stopped sandbox, and `audit` when nothing was ever created) |
| `4` | `wait --timeout` expired, job still running |
| `5` | `wait`: job lost (process gone: VM stopped or reset) |

`up` and `shell` return the agent's / shell's own exit status once attached. On a `2` or `1` error
the message is on stderr and stdout is empty, so check the exit code before parsing.

`agentbox status --json` (sandbox existence is probed with `sbx inspect`; disabled profiles aren't).
`job.state` is `none`, `running`, `finished` or `lost` (see *Headless jobs*); `exit_code` is set only when
finished. `job.vm` is the sandbox state **now** (`running`, `stopped`, `missing`, `unknown`), from one
`sbx ls --json` per `status` run, even when the job's own state is cached. `status` never boots a stopped sandbox: it asks `sbx ls --json`, and a job whose sandbox isn't
running is `lost` without any exec. A running sandbox with an unfinished job costs one `sbx exec`, and every
running sandbox one more for its interactive sessions: then, and only then, a profile also has `sessions`,
`activity_age_s` and `permission_mode`, as in `dashboard --json` (see *Dashboard*; process metadata, file
times and the permission mode only, never conversation content):

```json
{"profiles": [
  {"name": "h750", "valid": true, "enabled": true, "sandbox": "ab-h750", "agent": "claude",
   "mode": "direct", "sandbox_exists": true, "host_tools": ["flash-elf"], "host_tools_port": 47111,
   "bridge": {"running": true, "pid": 4242},
   "job": {"id": "20260101T120000Z-ab12cd", "state": "finished", "exit_code": 0,
           "session_id": "0b1c...", "started_at": "2026-01-01T12:00:01Z", "finished_at": "2026-01-01T12:07:45Z",
           "vm": "running", "branch": "fix-uart", "end_head": "3f9c...", "progress": {"...": "..."}},
   "sessions": [{"agent": "claude", "age_s": 10735}], "activity_age_s": 9, "permission_mode": "auto"},
  {"name": "scratch", "valid": true, "enabled": false, "sandbox": "ab-scratch", "agent": "shell", "mode": "clone",
   "job": {"id": null, "state": "none", "exit_code": null, "session_id": null, "started_at": null,
           "finished_at": null, "vm": null, "branch": null, "end_head": null, "progress": null}},
  {"name": "broken", "valid": false}
]}
```

`agentbox doctor [profile] --json` (exit `0` when `ok`, else `1`). `status` is one of `pass`, `fail`,
`warn`, `info`, `skip`; `profile` is `null` for host-level checks; `hint`, `details` and `data` are
optional. `version` is the agentbox release (also `agentbox --version`):

```json
{"ok": false, "version": "0.1.0", "checks": [
  {"profile": null, "status": "info", "message": "agentbox 0.1.0 (Python 3.13.1, /opt/homebrew/...)"},
  {"profile": null, "status": "pass", "message": "sbx present: sbx 1.2.3"},
  {"profile": "h750", "status": "warn", "message": "GH_TOKEN: looks like a real credential (40 chars)",
   "hint": "check `sbx secret ls` ...", "data": {"name": "GH_TOKEN", "class": "real", "length": 40}},
  {"profile": "h750", "status": "fail", "message": "no SSH agent forwarded", "hint": "restart the sbx daemon ..."},
  {"profile": "scratch", "status": "skip", "message": "skipped (disabled)"}
]}
```

`agentbox tools <profile> --json` (`params` are the `[tools.*]` param specs from the config;
the argv is not included):

```json
{"profile": "h750", "tools": [
  {"name": "flash-elf", "description": "Program an ELF via ST-LINK, verify, reset",
   "params": {"elf": {"type": "path", "suffixes": [".elf", ".hex", ".bin"]}},
   "extra_args": false, "timeout": 180, "confirm": true}
]}
```

## 4. Credentials per agent

Nothing is set globally. Each entry in a profile's `secrets` is applied with
`--sandbox ab-<profile>` after creation and removed on `down`.

agentbox never handles secret values itself and never puts one on a command line, where
`ps` or other process inspection could see it. It hands `sbx` the *source* (`ref` or
`command`) and `sbx` resolves it on the host; with neither, `sbx` prompts you itself.
Custom secrets must have a `ref` or `command`. Verbose mode redacts value flags anyway, and
agentbox refuses to run any `sbx` command that carries one.

```toml
secrets = [
  { service = "anthropic" },                                   # prompt once
  { service = "anthropic", ref = "op://Work/Anthropic/credential" },   # 1Password, resolved on host
  { service = "github", command = "some-cmd-printing-a-token" },       # run on host at request time
  { custom = true, host = "api.example.com", env = "EXAMPLE_KEY",
    ref = "op://Work/example/key" },                           # placeholder in VM, real value injected by proxy
  { custom = true, host = "api.example.com", env = "EXAMPLE_KEY",
    command = "security find-generic-password -s example -w" },  # or from the macOS keychain
]
```

- **Claude Code** — run `/login` inside the first sandbox. sbx keeps the OAuth token on the
  host and the VM only sees a sentinel. **The login is global to the host**: every sandbox uses it,
  and a `/login` in any sandbox replaces it for all of them, so you get one subscription at a time
  (to change account, `/login` again). Per-sandbox subscriptions don't work as of sbx v0.50.1:
  a `CLAUDE_CODE_OAUTH_TOKEN` custom secret loses to the global login (or isn't used), and
  `sbx secret set anthropic --sandbox …` sends an OAuth token as an `x-api-key`, which is rejected
  (docker/sbx-releases#11). A separate account per profile needs an API key
  (`service = "anthropic"` with a `ref`), billed per use.
- **Copilot CLI** — `service = "github"`. The proxy injects it for github.com and the Copilot
  API hosts, so **use a fine-grained PAT with only the Copilot Requests permission and no
  repository access**. Don't feed it `gh auth token`; that token can push to everything you can.
- **Junie** — not a built-in agent, so the `junie` kit installs it on the shell template.
  Either sign in with your JetBrains account inside the VM (token then lives in the VM —
  acceptable for a single-purpose key), or once you've seen in `sbx policy log` which host
  Junie sends its key to, use a `custom` secret so `JUNIE_API_KEY` in the VM is only a
  placeholder. The kit's allow-list is a starting point; tighten it from the policy log.
- **git** — by design there are **no push credentials** in the VM and no SSH agent. The agent
  commits; you review on the host and push. To let an agent pull public deps, allow
  `github.com` etc. without any credential.
- **Anything else** (vendor APIs, internal services): `custom` secret if it's a header
  token, otherwise don't give it to the agent.

`agentbox doctor <profile>` lists any `*KEY*/*TOKEN*/*SECRET*/*PASSW*` env vars visible inside
the VM and classifies each one *inside the VM*, so the value never leaves it: only the name,
a class and the length are printed. A value starting with a known real-token prefix (`ghp_`,
`gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`, `sk-ant-`, `sk-`, `xoxa-`/`xoxb-`/`xoxp-`, `AKIA`,
`glpat-`) is a **WARN** ("looks like a real credential"): check `sbx secret ls` for a global
secret, the profile's `env`, and the sbx agent template. Anything else is "placeholder/unknown",
which is what proxy-injected credentials should look like. `HOSTTOOLS_TOKEN` is skipped.

The check goes by **format only**. sbx gives the VM realistic-looking stand-in values: on one host
`GH_TOKEN` was flagged "looks like a real credential (40 chars)" although GitHub's API answered 401 Bad
credentials and sbx's policy log showed `api.github.com` as `forward-bypass` (no credential swapped in).
So a warning means "check where it comes from", not "it works". To check safely, make a read-only request
inside the sandbox; 401 means a dead placeholder:

```bash
curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $GH_TOKEN" https://api.github.com/user
```

## 5. Host tools (compilers, flashers, debuggers outside the VM)

The VM can't see your USB debug probe, and some tools (CubeProgrammer, licensed compilers)
are host-only. Profiles can expose a small set of host commands:

```toml
[tools.flash-elf]
description = "Program an ELF via ST-LINK, verify, reset"
argv = ["/Applications/.../STM32_Programmer_CLI", "-c", "port=SWD", "-w", "{elf}", "-v", "-rst"]
params.elf = { type = "path", suffixes = [".elf", ".hex", ".bin"] }
timeout = 180
lock = "probe"
```

Inside the VM:

```bash
hosttool list
hosttool flash-elf elf=build/h750.elf
hosttool fault-snapshot elf=build/h750.elf      # regs, bt, stack, CFSR/HFSR/MMFAR/BFAR
hosttool read-mem addr=0x24000000 words=64     # RAM/flash only, see below
```

How it works: `agentbox up` starts a bridge on `127.0.0.1:<port>` (and `::1`), adds
`localhost:<port>` to that sandbox's allow-list, and installs the `hosttool` client plus a
per-sandbox token in the VM. Because the workspace is mounted at the same absolute path,
a path the agent passes is the same file on the host. The bridge:

- runs a **fixed argv with no shell**; the agent fills only typed `{params}` (path / int /
  enum / regex-checked string), with optional, pattern-checked `extra_args`;
- resolves every path (symlinks included) and rejects anything outside the mounted workspaces;
- refuses to start if a tool's executable or fixed path args live inside a workspace;
- gives tools a clean environment (no host API keys), a timeout, an output cap, and a
  per-`lock` mutex so two agents can't hit the probe at once;
- runs tools from an **empty directory outside the workspace** by default, because many
  tools load config from `./` (OpenOCD's `openocd.cfg`, gdb's `.gdbinit`). Opt in per tool
  with `cwd = "{workspace}"` only if it's safe;
- can ask you first: `confirm = true` on a tool (or `confirm_host_tools = true` on a profile)
  pops a dialog on the host showing the exact command (`osascript` on macOS; `zenity` or `kdialog` plus a
  display on Linux, otherwise every confirmed call is denied; `kdialog` has no timeout); Deny or no answer within
  `confirm_timeout` (120 s) refuses the call. The example config confirms `flash-elf`.
  Without approval, the running agent can call any tool its profile lists, so keep the list
  short;
- rate-limits per tool (`max_calls` per `per_seconds`);
- restricts integer params to address `ranges`, with `span` so a whole read
  (`addr .. addr + words*4 - 1`) must sit inside one range. The example `read-mem` allows
  only the H750's ITCM, flash, DTCM and SRAM blocks: reading peripheral registers can have
  side effects (clearing status flags, popping a UART FIFO), so `0x40000000-0x5FFFFFFF` and
  `0xE0000000+` are out. Check the ranges against your part, and drop flash if it holds anything
  you don't want read;
- logs every call and rejection to `~/.local/state/agentbox/ab-<profile>/bridge.log`.

The bridge token lives in that state folder (and, by design, in the sandbox's environment).
agentbox creates everything there owner-only (`0700` folders, `0600` files), re-tightens
existing state on every run, and refuses to use a state folder owned by another user. A
process running as *you* on the Mac can still read it; that's the same trust level as your
SSH keys.

**The rule that matters:** a host tool that reads agent-writable input which can make it run
commands is host code execution. That includes `make`/`cmake`/`ninja` (build files),
gdb (`.gdbinit`, Python auto-loaded from an ELF section — hence `-nx` and
`set auto-load off` in the examples), compilers with free flags (`-fplugin`, `-B`, `-specs`,
`-wrapper`, `@file`), OpenOCD with a workspace `.cfg` (TCL). So:

- **compile inside the VM** (the `embedded-arm` kit installs arm-none-eabi-gcc, newlib,
  cmake, ninja, gdb-multiarch, cppcheck) and use host tools only for hardware;
- keep gdb command files and OpenOCD configs under `~/.config/agentbox/`, never the repo;
- flashing agent-built firmware onto a board on your desk is itself a decision — keep the
  `flash-*` tools in profiles where you want that, and the target on a bench supply.

Raw GDB remote from the VM (`target extended-remote host.docker.internal:3333`) needs
non-HTTP TCP to host loopback. sbx allows raw TCP only by explicit IP:port rule and blocks
host loopback by default, so treat that as an experiment
(`sbx policy allow network localhost:3333`, then check `sbx policy log`) rather than the plan.

## 6. Git workflow and the audit

**direct mode** (needed for host tools): the agent edits your working tree **and `.git/`**.
That matters because `.git/config` can make *any* git command run a program: `git status`
runs `core.fsmonitor`, `git diff` runs textconv/external diff drivers, checkout runs
smudge filters, commit runs hooks (from a repo-level `core.hooksPath` too, which overrides
your global one), fetch/push can run `uploadpack`/`sshCommand`, aliases can be `!shell`.
So after a session:

```bash
agentbox audit h750
```

The audit never runs git or any other tool on the host. It hashes files with Python, reads
`.git/config` (plus `config.worktree` and anything it `include`s) as text and lists every
setting that can run a command or redirect git, marking ones that are **NEW** since the
sandbox was created. `git status` is run inside the sandbox. `down` and `fresh` run the same
audit first. Symlinks and FIFOs are recorded, never followed or opened.

If the audit shows NEW git-config entries, open `.git/config` in a plain editor and fix it
**before** running git on the host. Review the code itself inside the sandbox
(`agentbox shell h750` → `git diff`, or `agentbox code h750`), not in a local terminal or a
local VS Code window (its git integration runs `git status` as soon as you open the folder).

Also set `git config --global core.hooksPath ~/.config/git/hooks` so repo-local hooks don't
run on the host. Note that a repo-level `core.hooksPath` overrides this, which is why the
audit flags it.

For repos you don't trust at all, use `mode = "clone"`: nothing on the host changes until
you fetch from the sandbox remote. Fetching runs no code from the sandbox's config.

**clone mode**: the agent works on its own clone inside the VM; nothing on the host changes
until you fetch from `sandbox-ab-<profile>` and merge. Safest for untrusted repos, but no host tools.
sbx adds that remote to the host repo while the sandbox runs and removes it when the sandbox stops
(about 30 s after the last session ends). The commits you fetched stay in your object store.

```bash
agentbox fetch h750               # the job's branch into review/h750-<branch>; add --ref R, --branch B, --start, --force, --json
git diff HEAD...review/h750-fix   # review (fetch prints the branch name)
git merge --ff-only review/h750-fix   # the only step that changes your branch, and you run it
```

`fetch` is for clone-mode profiles only; for a direct-mode profile it exits 2 and points at `audit`. It
refuses a stopped sandbox unless you pass `--start`, keeps one `sbx exec ... sleep` session open while it
works (and always ends it), waits up to 60 s for the remote, then fetches `--ref` (default: the branch the
latest job ended on, else `main`) from the sandbox into `--branch` (default `review/<profile>-<ref>` for a
job's branch, `review/<profile>` for `main`; see *Which branch* in §3). It names the remote itself; it never takes one from
anything the VM printed. Both names are checked as branch names and passed to git after `--`. It refuses
to touch a branch that is checked out in the host repo (exit 2), and refuses an update that is not a
fast-forward of the existing review branch (exit 1) unless you pass `--force`. Everything git prints is
passed through `safe_text`, since commit subjects and file names are agent-written. With `--json` the
result is one document: `profile`, `sandbox`, `remote`, `ref`, `branch`, `ref_source` (`job`, `flag` or
`default`), `commit`, `previous`,
`fast_forward_from_head`, `commits` (at most 50, each `{hash, subject}`), `commits_total`,
`files_changed`, `insertions`, `deletions` and `job_running`.

`fetch` and `review` (below) are the only places agentbox runs git on the host. That is safe because in clone mode the host
repo is mounted read-only, so the agent cannot have written its `.git/config`, and fetching from the
sandbox remote runs nothing from the VM's config. This holds only if no other profile mounts the same
folder writable (as a direct-mode workspace, or an `extra_workspace` without `:ro`). If one does, an
agent there could have changed `.git/config`, so run `agentbox audit` for that profile first. The diff is run with `--no-ext-diff --no-textconv`.
The raw equivalent, while the sandbox is running, is
`git fetch sandbox-ab-<profile> <branch>:review/<profile>-<branch>`.

### Reviewing and merging: `agentbox review`

```bash
agentbox review h750        # full screen; needs a terminal and a clone-mode profile
agentbox review             # every clone-mode profile's items together
agentbox review --json      # the items and their statuses (no diff): what the Rex action reads
agentbox history h750       # the jobs behind them, newest first (--json)
```

`review` is the owner's merge tool, a pull-request page in the terminal (Python's `curses`, like
`dashboard -i`). Agents never run it. On the left, one item per review branch (`review/<p>` and
`review/<p>-*`) and per recorded job whose branch has not been fetched yet, actionable ones first, newest
first: profile and branch, the brief's title, when, and a status:

| status | meaning | key |
|---|---|---|
| not fetched | the job ended on a branch, and no review branch has it yet | `f` runs `agentbox fetch <p>` (shown first; `y` runs it, in this terminal) |
| ready | the review branch has commits the target lacks, and a fast-forward is possible | `m` merges |
| diverged | the target has moved on: no fast-forward | `m` offers a merge commit |
| merged | the branch's tip is already in the target | `d` deletes the review branch |

The **target** is the branch checked out in the profile's primary workspace (in the header). On the right,
three tabs (Tab, or 1 2 3): **Overview** (the brief's title, job id, start, duration, exit, cost, turns, tool
calls, denials, branch and tip, "N commits, M files changed, +a −d", and the agent's final summary under
"the agent's own summary, not verified"), **Commits** (hash, subject, date; Enter shows that commit's diff,
`a` goes back) and **Files and diff**: a file list with +/− counts (at the left on wide panes) and the
merge-base diff `target...branch`, as a pull request shows it. ↑ ↓ PgUp PgDn and the wheel scroll it, `n`/`N`
jump between files, `]`/`[` between hunks, `/` searches, `w` wraps long lines (otherwise they are cut, with
`…`). Additions are green, deletions red, hunk headers cyan, file headers bold, in the terminal's own colours.
← → (or a click) move between the list and the tab; `?` lists every key.

- **`m` merge** (ready or diverged) shows the exact command and the commit count, and checks the workspace
  first. It refuses, saying why, if tracked files have staged or unstaged changes (untracked files are fine:
  git refuses by itself if one would be overwritten), if a merge, rebase, cherry-pick or revert is in progress,
  or if the branch or the target moved since the list was read. A fast-forward is
  `git merge --ff-only -- <branch>` after `y`. A diverged branch gets `git merge --no-ff --no-edit -- <branch>`,
  and only after you type `merge` and Enter; if that conflicts, `git merge --abort` runs at once and the
  conflicted files are listed. Afterwards it shows the target's new tip.
- **`d` delete** (merged only): `git branch -d -- <branch>` after `y`; lower-case `-d`, so git itself
  refuses a branch that isn't merged.
- **`o`** opens the diff in VS Code, one file at a time (`git difftool` with `code --wait --diff`, on
  temporary copies outside the repo), if `code` is on `PATH`; otherwise it says so.
- **`t`** opens a shell in the workspace, so you can run your install or tests after a merge: a new Rex tab
  (`rex run --cwd <workspace> --label "<p> shell"`, your login shell) inside Rex, else `$SHELL` in this
  terminal until you exit it. `review` itself never runs a project script, install or test.
- `r` refreshes, `q` quits and gives the terminal back (and its title).

It never rebases, resets, forces, pushes or checks out. git runs with argv lists, `--no-pager`,
`-c core.quotePath=true -c diff.noprefix=false`, and `--no-color --no-ext-diff --no-textconv` for anything it
draws; file names are read from `--numstat -z` and other NUL-separated output. Diffs, file names, commit
messages, the summary and the title are agent-written, so all of it is cleaned before it is drawn: no
escape sequence reaches your terminal. A file's diff shows at most 20,000 lines, then "(N more lines; open
in VS Code)"; binary files show as "binary". Branch names are checked as branch names before git sees them.
A merge runs your repo's own hooks (e.g. `post-merge`), as `git merge` does in a terminal.

**History.** When agentbox first sees a job ended (in `wait`, `progress`, `status`, `fetch`, the dashboard...)
while the sandbox is still up, it appends one record to `$state_dir/<sandbox>/history.jsonl` (owner-only,
the last 200 jobs): id, times, exit code, branch and end commit, turns, tool calls, denials, tokens, cost,
commits, the brief's title (its first Markdown heading, else its first line; at most 200 characters) and the
agent's final summary (the `result` of its stream, at most 8 KB). This is what `review` shows, because sbx
stops the VM, and the summary with it, about 30 s after a job ends. A job that was lost, or never seen
ending while its VM was up, gets a record with what is known.

## 7. What this protects and what it doesn't

| | |
|---|---|
| Protected | host filesystem outside mounts; host Docker; LAN, private IPs and host localhost (except the bridge port and a `local_model` port); other sandboxes; API keys/OAuth tokens (proxy-injected); SSH keys; git push rights; egress to non-allowed hosts |
| Not protected | anything in a rw mount (incl. `.git/`); data sent to allowed hosts (an agent can upload your code to any host you allow — keep allow-lists short); what host tools do with agent-supplied files; tokens you choose to put in the VM (Junie login, `env`) |

Text that comes out of the VM is agent-controlled, so agentbox never prints it raw: file names and git
config values in `audit`, `logs` (rendered and `--raw`), `doctor`'s in-sandbox lines, and `sbx` stderr
echoed in errors all go through one `safe_text()` that turns control characters (ESC, CR, BEL, DEL, the
C1 range, line-separator and bidi-override characters) into visible `\xNN` / `\uNNNN` escapes. Otherwise
an escape sequence in a file name or log line could rewrite your screen or write your clipboard (OSC 52).
`--json` output is already escaped by the JSON encoder. Tab and newline are kept.

### Permission mode (Claude)

The sandbox is the boundary: the agent can do anything inside the VM, and nothing outside it beyond what
the profile grants. Inside it, Claude profiles run Claude Code in **auto mode** by default
(`permission_mode = "auto"`): Claude Code's classifier approves routine actions and blocks risky ones (say,
force-pushing, or deleting things outside the task), as a second layer. It does not replace the sandbox.
`permission_mode = "bypass"` is the old behaviour, `--dangerously-skip-permissions`: no checks at all,
for when auto mode gets in the way. `doctor` shows the mode and warns about `bypass`.

- `run`: `--permission-mode auto` (or `--dangerously-skip-permissions`) on the headless argv. When auto
  mode is not available (plan, model, settings or Claude Code version), Claude Code carries on in another
  mode instead of failing; `run` sees that in the stream's first (`init`) event, stops the job and exits 1
  with a hint to set `bypass`. A Claude Code too old to know the mode exits at once, and `run` reports
  that the same way.
- `up`, `fresh` and `resume` pass `--permission-mode=auto` (or `--dangerously-skip-permissions`) to the
  agent after sbx's `--`, so the session starts in that mode whatever sbx's Claude agent defaults to.
  Claude Code shows the mode in its status line; Shift+Tab cycles modes.
- **Denials.** An action auto mode refuses comes back to the agent as an error tool result
  ("Permission for this action was denied by the Claude Code auto mode classifier. Reason: ...");
  the agent may try another way. agentbox counts them: `denials` in `progress --json` and
  `job.progress` of `status --json`, a `DENIED n` line in `progress`, `n denied` on `status` lines, a
  `denied` line in the dashboard's details, and `denied: <reason>` lines in `logs`. A job that keeps
  being denied is not failed for it; look at its log and decide.
- The key is refused on non-Claude profiles (opencode's `--auto` is unrelated: it approves opencode's
  own tool permissions and stays on).

## 8. Troubleshooting

- **Something blocked** → `sbx policy log`; add the host to the profile's `allow`, then
  `agentbox fresh <profile>` (kit rules apply at creation).
- **`hosttool: cannot reach host bridge`** → `agentbox status`; is the bridge running?
  `sbx policy log` should show `localhost:<port>` allowed. Bridge output:
  `~/.local/state/agentbox/ab-<profile>/bridge.out`.
- **Kit errors at create** → `sbx kit inspect <dir>` (`sbx kit validate` does not take v3 source
  kits). The kits use kit spec v3 (see *Kits* above); `cannot mix v3 kits with v1/v2 kits` means a
  kit is still a v2 `spec.yaml` — re-run the installer for the bundled ones. sbx kits are marked
  experimental and the schema has changed more than once, so re-run `agentbox doctor` after sbx
  upgrades. The generated per-sandbox kit (`~/.local/state/agentbox/ab-<profile>/kit/agentbox/`) is
  built in one function (`local_kit_spec`) if it needs adjusting.
- **Changed the profile but nothing happened** → `agentbox fresh <profile>`.
- **A skill isn't linked** → `agentbox doctor <profile>` says which and why (not mounted: needs `fresh`; name taken by a folder in the VM's `~/.claude/skills`; a rule violation in the set).
- **`doctor` says "skill X is not linked (not-mounted)"** after you added a skill set to a profile: mounts
  exist only from sandbox creation, so run `agentbox fresh <profile>`.
- **`down` / `fresh` say stdin is not a terminal**: sbx asks for confirmation before removing a sandbox. Pass
  `--force`, which also kills a running job and deletes the sandbox even while it is in use.
- **The fetch remote `sandbox-ab-<profile>` is missing**: the sandbox is stopped, and sbx removes the remote
  when it stops. Use `agentbox fetch <profile> --start`.
- **Verbose** → `verbose = true` in `[settings]` prints every `sbx` command.

## Development

Tests use only the standard library and never call the real `sbx` (any attempt fails the test):

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/agentbox share/hosttool && bash -n install.sh && sh -n share/agentbox-launcher && sh -n share/jobwrap && sh -n share/jobprogress
```

41 tests are skipped on macOS: they run the VM-side `sh` scripts
(the job wrapper, the probes, the kill script), which use Linux tools such as `setsid` and `/proc`. The
Ubuntu CI job runs them. The `fetch` tests use real temporary git repos and need `git`.

CI (`.github/workflows/ci.yml`) runs the same on Ubuntu and macOS with Python 3.11 and 3.14.

### Releasing

1. A PR that sets `VERSION` in `bin/agentbox` to the new `X.Y.Z`; merge it once CI passes.
2. Once `main`'s CI passes, tag that commit and publish the release:
   `git tag -a vX.Y.Z -m "agentbox X.Y.Z" && git push origin vX.Y.Z`, then `gh release create vX.Y.Z --verify-tag
   --prerelease` with notes.
3. `tools/release-checksums.sh vX.Y.Z` attaches `SHA256SUMS` (GitHub's tarball and zip for the tag). The one-line
   installers refuse a release without it.
4. In `sedurCode/homebrew-agentbox`, a PR updating `url` and `sha256` in `Formula/agentbox.rb` (the tarball's
   line from `SHA256SUMS`); merge once its CI passes.

## Licence

agentbox is dual licensed under the GNU AGPLv3 or a commercial licence. See [LICENSING.md](LICENSING.md) for an explanation and [LICENSE](LICENSE) for the AGPLv3 text.
