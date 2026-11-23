# agentbox in Rex

[Rex](https://www.superlogical.com/rex/docs) runs on the host only. `install.sh` copies these files to
`~/.local/share/agentbox/share/rex/` and never touches `~/.config/rex/`.

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
| `agentbox_dashboard` | session `agentbox` running `agentbox dashboard --watch` |
| `agentbox_fetch` | `agentbox fetch <profile>` in a visible tab |
| `agentbox_down` | `agentbox down <profile>` in a visible tab (sbx asks for confirmation there) |

The actions switch the Rex app to the session they open. Doing that from the `rex` CLI needs Rex's Remote
Control setting.

No keys are bound by default. Suggested bindings (use Rex's key-binding syntax in `init.lua`):

| keys | action |
|---|---|
| cmd-shift-a | `agentbox_open` |
| cmd-shift-j | `agentbox_run` |
| cmd-shift-d | `agentbox_dashboard` |

## Status

Inside Rex (`REX_SESSION` set, output on a terminal), `agentbox progress --watch`, `dashboard --watch` and
`wait` report job state with OSC 7501, so it shows in the tab and in View Activity: `working` (with plan
progress), `blocked` when the job has stalled, `done`, `error`, `idle`. Every record's id is the profile
name. The dashboard keeps one record per profile that follows its latest job, also after sbx has stopped
the sandbox (read from the state agentbox keeps on the host; a stopped sandbox is never booted): `done` or
`error` stays until the profile's next job starts, and a job that died with its sandbox shows `error`
("sandbox stopped"). The record is cleared when the profile has no job, is disabled or is removed. `AGENTBOX_NO_OSC=1` turns the reports off. A report holds only host-side state
(job state, exit code, profile name, agentbox's own wording), never text from the sandbox.

## Notifications

`notify.lua` posts a macOS notification when a profile's record becomes blocked, done or error. It keeps
the state per profile, not per tab, so a job watched from several tabs (the dashboard and a
`progress --watch` tab, say) notifies once; the next job (`working`) or an `idle` report re-arms it. Start
it in a tab you keep open:

```sh
rex do ~/.local/share/agentbox/share/rex/notify.lua --all
```
