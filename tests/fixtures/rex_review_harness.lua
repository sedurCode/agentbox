-- A stand-in for Rex's Lua API that runs agentbox_review once and prints what it asked Rex to do.
-- usage: lua rex_review_harness.lua <dir of agentbox.lua> new|all|existing|existing_window
package.path = arg[1] .. "/?.lua;" .. package.path
local scenario = arg[2]
local calls, actions = {}, {}
local function log(...) local t = {} for i = 1, select("#", ...) do t[i] = tostring(select(i, ...)) end print(table.concat(t, " ")) end
local sessions = {}
if scenario == "existing" or scenario == "existing_window" then
  sessions = { { session_id = "S1", label = "ab:p",
                 windows = scenario == "existing_window" and { { window_id = "W9", window_label = "Review" } } or {} } }
end
rex = {
  log = function(level, msg) log("LOG", level, msg) end,
  exec = function(argv, o) log("EXEC", table.concat(argv, " ")); return { ok = true, stdout = "{}" } end,
  json = { decode = function(s) return { profiles = {
    { name = "p", counts = { ready = 2, not_fetched = 1, diverged = 0, merged = 3 } },
    { name = "q", counts = { ready = 0, not_fetched = 0, diverged = 0, merged = 0 } },
    { name = "Bad Name", counts = {} } } } end },
  ui = { pick = function(o)
    for _, it in ipairs(o.items) do log("ITEM", it.id, it.title, it.subtitle) end
    return scenario == "all" and "*" or "p" end },
  call = function(m, a)
    if m == "session.list" then return { sessions = sessions } end
    if m == "window.list" then error("no such method") end
    if m == "session.create" then
      local w = a.initial_windows[1]
      log("CREATE", a.label, w.window_label, table.concat(w.layout.block.options.command, " "), tostring(w.layout.block.options.exit and w.layout.block.options.exit.on_completion))
      return { session_id = "NEW" }
    end
    if m == "session.new_window" then log("NEWWIN", a.session_id, a.window_label, table.concat(a.layout.block.options.command, " ")); return {} end
    log("CALL", m)
  end,
  client = { queue = function(m, a) log("QUEUE", m, a.session_id, a.window_id or "") end },
  layout = { block = function(b) return { block = b } end },
  action = function(a) actions[a.name] = a end,
}
require("agentbox").setup{ agentbox = "/ab" }
actions.agentbox_review.run({})
