-- agentbox notifications for Rex. Installed by agentbox (install.sh) into ~/.local/share/agentbox/share/rex/.
-- Start once, in a tab you keep open:  rex do ~/.local/share/agentbox/share/rex/notify.lua --all
-- (The actions in agentbox.lua need these lines in ~/.config/rex/init.lua; this script does not:
--   package.path = os.getenv("HOME") .. "/.local/share/agentbox/share/rex/?.lua;" .. package.path
--   require("agentbox").setup{} )
--
-- Posts a macOS notification when an agentbox status record (app=agentbox, OSC 7501) becomes
-- blocked, done or error. Only the record id (a profile name) and the session label are shown;
-- message text is never passed on.

local NOTIFY = { blocked = "needs you", done = "finished", error = "failed" }
local last = {}   -- "<block>/<id>" -> last state notified, so a redraw doesn't notify again

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
  if rec.app ~= "agentbox" then return end
  local key = tostring(ctx.block_id) .. "/" .. tostring(ev.id or "")
  local what = NOTIFY[rec.state]
  if not what then last[key] = rec.state; return end
  if last[key] == rec.state then return end
  last[key] = rec.state
  local profile = valid_name(ev.id) and ev.id or nil
  local view = rex.session.view{} or {}
  local where = type(view.label) == "string" and view.label or "Rex"
  notify("agentbox", (profile and (profile .. " ") or "job ") .. what .. " (" .. where .. ")")
end)

rex.on("terminal.program_status_removed", function(ctx, ev)
  for _, id in ipairs(ev.ids or {}) do last[tostring(ctx.block_id) .. "/" .. tostring(id)] = nil end
end)
