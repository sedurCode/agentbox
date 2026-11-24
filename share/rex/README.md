# agentbox in Rex

[Rex](https://www.superlogical.com/rex/docs) runs on the host only. `install.sh` (or `install.ps1`) copies these files to
`~/.local/share/agentbox/share/rex/` if it finds Rex (`$AGENTBOX_REX`, `rex` on `PATH`,
Rex.app or `~/.config/rex/`), and never touches `~/.config/rex/`. Installed Rex after agentbox? Run the installer again.

## Actions

Add to `~/.config/rex/init.lua`:

```lua
package.path = os.getenv("HOME") .. "/.local/share/agentbox/share/rex/?.lua;" .. package.path
require("agentbox").setup{}
```

`setup{ agentbox = "/abs/path/to/agentbox" }` points at another agentbox binary (the default is
`~/.local/bin/agentbox`). The command palette then has, under "agentbox":

| action | what it does |
|---|---|
| `agentbox_open` | pick a profile; session `ab:<profile>` with Agent (`agentbox up`/`resume` typed, not run), Progress and Shell tabs |
| `agentbox_run` | pick a profile and a prompt file; `run --detach`, then `progress --watch` in a Job tab |
| `agentbox_dashboard` | session `agentbox` running `agentbox dashboard --interactive` (keys below) |
| `agentbox_fetch` | `agentbox fetch <profile>` in a visible tab |
| `agentbox_review` | pick a clone-mode profile, or "All profiles" (each shows its counts, e.g. "2 ready · 1 not fetched", from `agentbox review --json`); `agentbox review [<profile>]` in a window labelled "Review" in session `ab:<profile>` (or `agentbox` for all), created if needed. If that session already has a Review window, it is selected instead of opening another |
| `agentbox_stop` | pick a running sandbox; `agentbox stop <profile>` in a visible tab. If a job runs there, the picker says it will be lost and the profile name must be typed exactly. Never `--force` |
| `agentbox_down` | `agentbox down <profile>` in a visible tab (sbx asks for confirmation there) |

The dashboard tab is a full-screen view of every profile. Select one with ↑ ↓ / j k or a click (the wheel
scrolls); Enter or `p` runs `agentbox progress <profile> --watch` in the tab and comes back when it ends or
on Ctrl-C; `l` shows the job's log in `$PAGER` (default `less -R`); `f` shows `agentbox fetch <profile>`, the
ref it will fetch (the branch the job ended on, else `main`) and the review branch, and runs it only after
`y` (a stopped sandbox gets a note instead); `u` opens `agentbox up <profile>` in a new tab of this session
(with `rex run`; the `rex` CLI is `$AGENTBOX_REX`, else `rex` on `PATH`, else the one in Rex.app; if that fails,
the next `u` runs it in the dashboard's own tab); `s` stops a running sandbox after `y`, or, if a job runs
there (it will be lost), after the profile name is typed; `v` opens `agentbox review <profile>` for a clone-mode profile
(in a new tab of this session, as `u` does; a ◆ in the list marks a profile with branches to review); `r` refreshes,
`?` lists the keys, `q` quits. It never runs `--force`, `--start`, `down`, `fresh`, `run` or `resume`.

`agentbox review` is the owner's merge tool (agents never run it): a pull-request page for clone mode's review
branches with Overview, Commits and Files and diff tabs, `f` fetch, `m` merge (`git merge --ff-only`, or a merge
commit after typing `merge`), `d` delete a merged branch, `o` VS Code, and `t`, which opens a shell in the
profile's workspace in a new tab (`rex run --cwd <workspace> --label "<profile> shell"`, your login shell) so you
can run your install or tests after a merge. While it runs, the tab's title is `review: <profile>/<branch>`
(restored when it quits). See the main README, "Reviewing and merging". `agentbox dashboard --watch` still gives the plain text view.

The actions switch the Rex app to the session they open. Doing that from the `rex` CLI needs Rex's Remote
Control setting.

No keys are bound by default. Suggested bindings (use Rex's key-binding syntax in `init.lua`):

| keys | action |
|---|---|
| cmd-shift-a | `agentbox_open` |
| cmd-shift-j | `agentbox_run` |
| cmd-shift-d | `agentbox_dashboard` |

## Status

Inside Rex (`REX_SESSION` set, output on a terminal), `agentbox progress --watch`, `dashboard --watch` or
`--interactive`, and `wait` report job state with OSC 7501, so it shows in the tab and in View Activity: `working` (with plan
progress), `blocked` when the job has stalled, `done`, `error`, `idle`. Every record's id is the profile
name, except `agentbox review`'s: one record with id `review`, `idle` with "N to review" while branches wait
for you, cleared when none do or when it quits. It is never `blocked`, `done` or `error`, so Rex never
notifies about an open review. The dashboard keeps one record per profile that follows its latest job, also after sbx has stopped
the sandbox (read from the state agentbox keeps on the host; a stopped sandbox is never booted): `done` or
`error` stays until the profile's next job starts, and a job that died with its sandbox shows `error`
("sandbox stopped"). The record is cleared when the profile has no job, is disabled or is removed.
While interactive agent sessions (`agentbox up`, "Open Profile") run in a sandbox and no headless job does,
its dashboard record is `working` with "interactive" when a session wrote in the last 90 seconds, else `idle`
with "interactive, idle 12m"; never `done`, `error` or `blocked`, so Rex doesn't notify about them. When the
sessions end the record follows the latest job again (as above), or is cleared if there is none. To see them
the dashboard reads only process metadata, file times and Claude's permission mode in the running VM, never
conversation content. `AGENTBOX_NO_OSC=1` turns the reports off. A report holds only host-side state
(job state, exit code, profile name, agentbox's own wording), never text from the sandbox.

## Notifications

Rex posts macOS notifications for agentbox's records by itself, only while Rex is in the background; set
them up under Settings > Notifications > Program Activity. Rex notifies once per tab that reports, so a job
watched from both a `progress --watch` tab and the dashboard notifies twice. If that's too much, turn off
notifications for one state (blocked, done or error) in Rex's settings.
