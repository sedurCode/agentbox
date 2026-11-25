# agentbox in VS Code

The interactive dashboard (`agentbox dashboard -i`) as a VS Code view. It is plain JavaScript with no
dependencies and no build step, and it reads `agentbox dashboard --json`, so it shows what the terminal
dashboard shows.

## Install

```bash
agentbox vscode install       # also after upgrading agentbox; then reload VS Code's windows
agentbox vscode status        # current, outdated or missing (--json)
agentbox vscode uninstall
```

There is no build step and no node: `install` zips this directory into a `.vsix` (with the manifest VS Code
wants) and runs `code --install-extension` on it. The VS Code CLI is `[settings] vscode`, else `code` on
`PATH`, else the one in the app bundle, as for `agentbox code`.

To try it without installing: `code --extensionDevelopmentPath="$PWD/share/vscode"`. It needs an agentbox
that puts `state` in `dashboard --json` rows; with an older one the view asks you to upgrade agentbox.
It finds the CLI at `agentbox.path`, else the first that exists of `~/.local/bin/agentbox` (`agentbox.cmd` on
Windows) and Homebrew's (`/opt/homebrew/bin`, `/usr/local/bin`, `/home/linuxbrew/.linuxbrew/bin`), else
`agentbox` on `PATH`.

## What it shows

- **Profiles view** (the agentbox icon in the activity bar): every profile, sorted as in the dashboard (working
  jobs, working interactive sessions, those needing attention, idle sessions, finished, the rest). Each row
  has the state, how long the job has run or how long ago it ended, and the agent, with a ◆ when clone-mode
  branches are waiting for review. Expand a row for the VM's CPU, memory and disk, the interactive sessions
  and their permission mode, the job's turns, tools, tokens, cost and commits, the agent's plan (its own
  claim, not verified) and the review counts.
- **Status bar**: working jobs, interactive sessions, profiles needing attention and profiles with branches
  to review, highlighted when something has stalled, failed or been lost. A click opens the view.
- **Notifications** when a profile's job finishes, fails, stalls or is lost, with buttons for the log, fetch
  or progress. Not on the first refresh, so opening VS Code doesn't replay old results.

It refreshes every `agentbox.refreshInterval` seconds (default 5) while the view is visible, and every
`agentbox.backgroundInterval` seconds (default 30; 0 turns it off) while the view is hidden, for the status
bar and notifications. Each refresh costs what `agentbox dashboard --json` costs: one `sbx exec` per
running sandbox. Stopped sandboxes are never booted.

## Actions

On a profile's context menu (some also appear inline when you hover a row), or from the command palette
under "agentbox", which asks for a profile:

| action | runs |
|---|---|
| Watch Progress | `agentbox progress <profile> --watch` in a terminal |
| Show Job Log | `agentbox logs <profile>` in a read-only editor (ANSI and control characters removed) |
| Fetch for Review | `agentbox fetch <profile>` as a task, after a confirmation that names the ref. Clone mode and a running sandbox only |
| Review Branches | `agentbox review <profile>` in a terminal |
| Open Agent Session (up) | `agentbox up <profile>` in a terminal; asks first if a job is running there |
| Open Workspace in VS Code | `agentbox code <profile>`: a Remote-SSH window into the sandbox |
| Stop Sandbox | `agentbox stop <profile>` as a task, after a confirmation; if a job is running (it will be lost), only after you type the profile name |
| Open the Interactive Dashboard | `agentbox dashboard --interactive` in a terminal (view title bar) |

Like the terminal dashboard, it never runs `--force`, `--start`, `down`, `fresh`, `run` or `resume`. Every
command is started without a shell (on Windows, `cmd /c` for `agentbox.cmd`) and passes only a profile name
that matches agentbox's profile-name pattern. Text from the sandbox (plan steps, notes, branch names) has
control and bidi characters removed, and tooltips never render HTML or command links.
