-- agentbox notifications for Rex. Installed by agentbox (install.sh) into ~/.local/share/agentbox/share/rex/.
-- Start once, in a tab you keep open:  rex do ~/.local/share/agentbox/share/rex/notify.lua --all
-- (The actions in agentbox.lua need these lines in ~/.config/rex/init.lua; this script does not:
--   package.path = os.getenv("HOME") .. "/.local/share/agentbox/share/rex/?.lua;" .. package.path
--   require("agentbox").setup{} )
--
-- Posts a macOS notification when a profile's agentbox status record (app=agentbox, OSC 7501, id = the
-- profile name) becomes blocked, done or error. State is kept per profile, not per terminal, so a job
-- watched from several tabs (progress --watch, wait, the dashboard) notifies once; a working or idle
-- report lets the next job notify again. Records without a profile id are ignored. Only the profile
-- name and the session label are shown; message text is never passed on.

local NOTIFY = { blocked = "needs you", done = "finished", error = "failed" }
local last = {}   -- profile -> its record's last state, so another tab or a redraw doesn't notify again

local function valid_name(s)
  return type(s) == "string" and #s >= 1 and #s <= 41 and s:match("^[a-z0-9][a-z0-9%-]*$") ~= nil
end

local function notify(title, text)
  -- Values go in as AppleScript run arguments, never spliced into the script.
  rex.exec{ "osascript",
    "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
    "-e", "end run", title, text }
end

rex.on("terminal.program_status_changed", function(ctx, ev)
  local rec = ev.record or {}
  if rec.app ~= "agentbox" or not valid_name(ev.id) then return end
  local prev = last[ev.id]
  last[ev.id] = rec.state
  local what = NOTIFY[rec.state]
  if not what or prev == rec.state then return end
  local view = rex.session.view{} or {}
  local where = type(view.label) == "string" and view.label or "Rex"
  notify("agentbox", ev.id .. " " .. what .. " (" .. where .. ")")
end)
