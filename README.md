# agentbox — constrained sandboxes for coding agents on top of Docker Sandboxes (`sbx`)

agentbox runs coding agents in constrained Docker Sandboxes, with profiles, scoped credentials and a bridge to allow-listed host tools.

- **Requirements:** macOS, Docker Sandboxes (`sbx`), and Python 3.11+ (standard library only).
- **Status:** early. The `sbx` kit schema is experimental and may change.

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
- **Junie** kit (not built into sbx) and an **Arm embedded toolchain** kit.

```
 host (macOS)                                   microVM  ab-h750
 ───────────────                                ─────────────────────────────
 ~/.config/agentbox/agentbox.toml  (agent can't see)
 sbx daemon ── proxy ── allow-list + key injection ◄── all HTTP(S) egress
 agentbox bridge 127.0.0.1:47111 ◄──────────────── hosttool flash-elf elf=build/fw.elf
   └─ STM32_Programmer_CLI / st-info / arm-none-eabi-gdb  (USB probe lives here)
 ~/src/h750-firmware  ◄═══ direct mount (rw) ═══►  same path inside the VM
 ~/src/stm32-common   ◄═══ mount :ro ══════════►   same path, read-only
```

## 1. Install

```bash
brew trust docker/tap && brew install docker/tap/sbx
brew install python            # agentbox needs Python 3.11+ (stdlib only)
sbx login                      # Docker account; required by sbx
unzip agentbox.zip && cd agentbox && ./install.sh
```

`install.sh` puts the CLI in `~/.local/share/agentbox` (the script as `bin/agentbox.py`, plus a
small `sh` launcher `bin/agentbox`), links `~/.local/bin/agentbox`, and seeds `~/.config/agentbox/{agentbox.toml,kits/,gdb/}` without overwriting anything.

The launcher looks for Python at run time (`python3.14`, `3.13`, `3.12`, `3.11`, then `python3`;
the first that is 3.11+ wins), so removing or upgrading one interpreter doesn't break the
install. With none it prints "agentbox needs Python 3.11+ (brew install python)" and exits 1.

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

Edit `~/.config/agentbox/agentbox.toml`. The example has an STM32H750 profile per agent;
the important keys:

| key | meaning |
|---|---|
| `enabled` | `true` (default). `false` marks a template: `doctor` and `status` print "skipped (disabled)", `up`/`code`/`fresh` refuse it, `down`/`stop`/`tools`/`audit` still work |
| `agent` | `claude`, `copilot`, `codex`, `gemini`, `shell` (built-in) or a sandbox kit name/path (`junie`) |
| `workspace` | primary project, mounted read-write at the same absolute path |
| `extra_workspaces` | more mounts, append `:ro` for read-only |
| `mode` | `direct` (default) or `clone` — agent gets its own git clone, host tree untouched; you `git fetch sandbox-ab-<profile>` to review. Host tools need `direct`. |
| `kits` | kit names under `~/.config/agentbox/kits/` or paths |
| `allow` / `deny` | sandbox-scoped egress rules (merged with `[defaults]`; deny wins) |
| `env` | plain env vars for the VM (not for secrets) |
| `secrets` | sandbox-scoped credentials, see §4 |
| `keepalive` | `true` (default). `false`: `agentbox run` doesn't hold an exec session open while a job runs, so sbx may stop the sandbox under a detached job (see *Headless jobs*) |
| `host_tools`, `host_tools_port` | which `[tools.*]` this sandbox may call, and a unique port per profile |
| `instructions` | extra text appended to the agent's instruction file inside the VM |

Kits shipped in `kits/` and seeded into `~/.config/agentbox/kits/` (existing ones are kept):

| kit | what it does |
|---|---|
| `embedded-arm` | arm-none-eabi GCC/newlib, CMake, Ninja, gdb-multiarch, cppcheck, clang-format |
| `junie` | JetBrains Junie CLI as an agent (`agent = "junie"`) |
| `claude-team` | Claude model routing, **Claude profiles only**: `ANTHROPIC_MODEL=opusplan` (Opus when planning, Sonnet when executing), `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (no telemetry/error reporting; blocked by the allow-list anyway), plus user-level subagents `reader` (Haiku, read-only lookup), `worker` (Sonnet, scoped edits and tests) and `architect` (Opus, read-only design/review), and delegation instructions. Use as `kits = [..., "claude-team"]` |
| `python-node` | Python 3 with venv/pip, Node and git, plus the Ubuntu/Debian package hosts needed to install them |

**Keep the config and kits out of every workspace.** A kit in the repo is agent-writable,
so an agent could widen its own allow-list or add host tools for next time. The bridge
refuses to start if the config or kits dir sits inside a mounted workspace.

Then:

```bash
agentbox doctor            # validates kits, host tools, paths
```

## 3. Daily use

```bash
agentbox up h750           # first time: create + scoped secrets + attach. Later: re-attach
agentbox up h750 -- --resume   # extra args go to the agent
agentbox shell h750        # bash inside the VM
agentbox tools h750        # what the agent can run on the host
agentbox audit h750        # what changed in hooks/build/IDE/agent config since creation
agentbox stop h750         # pause sandbox + bridge
agentbox down h750         # audit, delete sandbox, scoped secrets, local state (--force kills a running job)
agentbox fresh h750        # down + up — use after editing the profile (kits apply at creation)
agentbox status
```

A new sandbox takes a little while on first create (kit installs run); re-attaching is quick.
For throw-away work: `agentbox fresh <profile>` every time.

Inside the sandbox agents run in their "skip permissions" mode — the VM is the boundary.
Use `Shift+Tab` / `/permissions` in Claude Code if you want prompts back.

### Headless jobs

For scripted or long unattended runs (VoiceDev's gateway, CI-like use) `agentbox up -- -p "..."` is
fragile: it needs an attached terminal, so the `sbx run` client dies on daemon timeouts and the
output is lost. `agentbox run` starts the agent *inside* the VM, detached from any session, and
keeps its state in files you can poll. Supported agent: `claude` (profiles with `agent = "claude"`).

```bash
agentbox run h750 --prompt-file task.md --detach     # prints the job id, returns at once
agentbox wait h750 --timeout 3600                    # block; exit status = the job's exit status
agentbox logs h750 -f                                # follow, one human line per event
agentbox logs h750 --raw                             # the raw stream-json lines
agentbox resume h750                                 # interactive `claude --resume=<session id>`
agentbox run h750 --prompt-file task.md              # without --detach: run, then follow logs, then exit with the job's status
agentbox run h750 --prompt-file - --agent-args --model opus < task.md
```

- **Prompt handling.** The prompt is sent into the VM on the `sbx exec` *stdin* and stored in
  `prompt.md` (mode `0600`); the agent reads it on stdin. agentbox never puts it on an `sbx` or VM
  command line. `--prompt-file` (or `-` for stdin) is preferred over `--prompt TEXT`, because
  `--prompt` text is part of *your own* `agentbox` command line on the Mac. `--agent-args ...`
  takes the rest of the line; the profile's `agent_args` (meant for interactive `up`) are not used.
- **What runs.** `claude -p --output-format stream-json --verbose --dangerously-skip-permissions`
  (the VM is the boundary, as for `up`), in the primary workspace, via `setsid nohup`, so the job
  survives the exec session that started it.
- **Job directory** (inside the VM, `~/.agentbox/jobs/<id>/`, id like `20260101T120000Z-ab12cd`):
  `prompt.md`, `wrap.sh` (the wrapper), `pid`, `started_at`, `output.jsonl` (the stream),
  `stderr.log`, `finished_at`, `exit_code` (written last, atomically, by the wrapper) and
  `session_id` (taken from the stream's init message). The host keeps only
  `~/.local/state/agentbox/ab-<profile>/job.json` (current job id, session id, and the result once seen).
- **Job state** comes from those files only: `finished` if `exit_code` exists; `running` if the
  `pid` file's process is alive **and** `/proc/<pid>/cmdline` runs this job's `wrap.sh` (so a
  reused pid doesn't count); otherwise `lost`. Processes are never found with `pgrep -f`, which
  matches its own command line. A **VM reboot, `stop`, or `down` loses a running job**: its state
  becomes `lost` (the files may remain after a reboot; the process doesn't).
- **Session ids** are validated (`[A-Za-z0-9][A-Za-z0-9_-]{7,79}`, so never an option-looking `-...`) wherever they
  are read from the VM, and passed as the single argument `--resume=<id>`.
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
  `logs` exits 3 instead of booting; `logs --start` boots it to read the output.
- **Retries.** `run`, `wait` and `logs` retry transient `sbx` errors (`context deadline exceeded`,
  connection reset/refused) with exponential backoff (1, 2, 4, 8 s; five attempts). A failed
  `create` is not repeated blindly: agentbox first checks whether the sandbox now exists.
- **`down` / `fresh`** refuse while a job is running; `--force` kills it (the wrapper's process
  group, or its children and then the wrapper if there is no `setsid`, after checking the pid is really this job's wrapper) and removes the sandbox anyway.

Exit codes for `wait` (and the final status of a foreground `run`): the **job's own exit code**
(claude: `0` success, `1` error) when it finished; `4` = `--timeout` expired with the job still
running; `5` = job lost. Codes 1-3 keep their meaning from the table above for agentbox's own
errors, so a job that exits with 2-5 is ambiguous: read `status --json` (`job.exit_code`) if you need to tell.

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

### Scripting agentbox

`status`, `doctor` and `tools` take `--json`: stdout is then exactly one JSON document, the human
text is unchanged without the flag, and diagnostics still go to stderr. No secret values appear
in any of them (`doctor` reports token-like variables by name, class and length only).

**Exit codes** (all commands):

| code | meaning |
|---|---|
| `0` | ok |
| `1` | checks failed (`doctor`) or a command failed (`sbx` error, bridge, SSH, ...) |
| `2` | usage or config error: bad arguments, no/invalid config, unknown or disabled profile, unsafe tool definition |
| `3` | sandbox not found (`shell`, `stop`, `resume`, and `audit` when nothing was ever created) |
| `4` | `wait --timeout` expired, job still running |
| `5` | `wait`: job lost (process gone: VM stopped or reset) |

`up` and `shell` return the agent's / shell's own exit status once attached. On a `2` or `1` error
the message is on stderr and stdout is empty, so check the exit code before parsing.

`agentbox status --json` (sandbox existence is probed with `sbx inspect`; disabled profiles aren't).
`job.state` is `none`, `running`, `finished` or `lost` (see *Headless jobs*); `exit_code` is set only when
finished. `job.vm` is the sandbox state **now** (`running`, `stopped`, `missing`, `unknown`), from one
`sbx ls --json` per `status` run, even when the job's own state is cached. `status` never boots a stopped sandbox: it asks `sbx ls --json`, and a job whose sandbox isn't
running is `lost` without any exec. A running sandbox with an unfinished job costs one `sbx exec`:

```json
{"profiles": [
  {"name": "h750", "valid": true, "enabled": true, "sandbox": "ab-h750", "agent": "claude",
   "mode": "direct", "sandbox_exists": true, "host_tools": ["flash-elf"], "host_tools_port": 47111,
   "bridge": {"running": true, "pid": 4242},
   "job": {"id": "20260101T120000Z-ab12cd", "state": "finished", "exit_code": 0,
           "session_id": "0b1c...", "started_at": "2026-01-01T12:00:01Z", "finished_at": "2026-01-01T12:07:45Z",
           "vm": "running"}},
  {"name": "scratch", "valid": true, "enabled": false, "sandbox": "ab-scratch", "agent": "shell", "mode": "clone",
   "job": {"id": null, "state": "none", "exit_code": null, "session_id": null, "started_at": null,
           "finished_at": null, "vm": null}},
  {"name": "broken", "valid": false}
]}
```

`agentbox doctor [profile] --json` (exit `0` when `ok`, else `1`). `status` is one of `pass`, `fail`,
`warn`, `info`, `skip`; `profile` is `null` for host-level checks; `hint`, `details` and `data` are
optional:

```json
{"ok": false, "checks": [
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
  host and the VM only sees a sentinel; later sandboxes reuse it. Or use `service = "anthropic"`.
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
  pops a macOS dialog showing the exact command; Deny or no answer within
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
sbx removes that remote when the sandbox stops, so `sandbox-ab-<profile>/main` stops resolving (the commits
stay in your object store). Fetch into a local branch, review, then fast-forward:

```bash
git fetch sandbox-ab-<profile> main:review/<profile>
git log review/<profile>            # review
git merge --ff-only review/<profile>
```

## 7. What this protects and what it doesn't

| | |
|---|---|
| Protected | host filesystem outside mounts; host Docker; LAN, private IPs and host localhost (except the bridge port); other sandboxes; API keys/OAuth tokens (proxy-injected); SSH keys; git push rights; egress to non-allowed hosts |
| Not protected | anything in a rw mount (incl. `.git/`); data sent to allowed hosts (an agent can upload your code to any host you allow — keep allow-lists short); what host tools do with agent-supplied files; tokens you choose to put in the VM (Junie login, `env`) |

Text that comes out of the VM is agent-controlled, so agentbox never prints it raw: file names and git
config values in `audit`, `logs` (rendered and `--raw`), `doctor`'s in-sandbox lines, and `sbx` stderr
echoed in errors all go through one `safe_text()` that turns control characters (ESC, CR, BEL, DEL, the
C1 range, line-separator and bidi-override characters) into visible `\xNN` / `\uNNNN` escapes. Otherwise
an escape sequence in a file name or log line could rewrite your screen or write your clipboard (OSC 52).
`--json` output is already escaped by the JSON encoder. Tab and newline are kept.

## 8. Troubleshooting

- **Something blocked** → `sbx policy log`; add the host to the profile's `allow`, then
  `agentbox fresh <profile>` (kit rules apply at creation).
- **`hosttool: cannot reach host bridge`** → `agentbox status`; is the bridge running?
  `sbx policy log` should show `localhost:<port>` allowed. Bridge output:
  `~/.local/state/agentbox/ab-<profile>/bridge.out`.
- **Kit errors at create** → `sbx kit validate <dir>`. The kits use the v2 kit grammar
  (`schemaVersion: "2"`, `permissions.network.allow`, `setup.install`); sbx kits are marked
  experimental and the schema has changed twice this year, so re-run `agentbox doctor` after
  sbx upgrades. The generated per-sandbox kit is rendered in one function
  (`render_local_kit`) if it needs adjusting.
- **Changed the profile but nothing happened** → `agentbox fresh <profile>`.
- **Verbose** → `verbose = true` in `[settings]` prints every `sbx` command.

## Development

Tests use only the standard library and never call the real `sbx` (any attempt fails the test):

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/agentbox share/hosttool && bash -n install.sh && sh -n share/agentbox-launcher
```

CI (`.github/workflows/ci.yml`) runs the same on Ubuntu and macOS with Python 3.11 and 3.14.

## Licence

agentbox is dual licensed under the GNU AGPLv3 or a commercial licence. See [LICENSING.md](LICENSING.md) for an explanation and [LICENSE](LICENSE) for the AGPLv3 text.
