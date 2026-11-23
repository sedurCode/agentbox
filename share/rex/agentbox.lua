-- agentbox actions for Rex. Installed by agentbox (install.sh) into ~/.local/share/agentbox/share/rex/.
--
-- In ~/.config/rex/init.lua:
--   package.path = os.getenv("HOME") .. "/.local/share/agentbox/share/rex/?.lua;" .. package.path
--   require("agentbox").setup{}            -- optional: agentbox = "/abs/path/to/agentbox"
--
-- Actions (command palette, category "agentbox"):
--   agentbox_open       pick a profile -> session "ab:<profile>" (Agent, Progress, Shell tabs)
--   agentbox_run        pick a profile + prompt file -> run --detach, then progress --watch
--   agentbox_dashboard  session "agentbox" running `agentbox dashboard --interactive`
--   agentbox_fetch      fetch in a visible tab (agentbox prints what it did)
--   agentbox_stop       stop a running sandbox in a visible tab (a running job: type the name; never --force)
--   agentbox_down       down in a visible tab (sbx asks for confirmation there; never --force)
--   agentbox_review     pick a clone-mode profile (or all) -> `agentbox review` in a "Review" window

local M = {}

local TERMINAL = "com.superlogical.terminal.shell"
local HOME = os.getenv("HOME") or ""
local opts = { agentbox = HOME .. "/.local/bin/agentbox" }

-- agentbox's NAME_RE: ^[a-z0-9][a-z0-9-]{0,40}$
local function valid_name(s)
  return type(s) == "string" and #s >= 1 and #s <= 41 and s:match("^[a-z0-9][a-z0-9%-]*$") ~= nil
end

local function warn(msg)
  rex.log("warn", "agentbox: " .. msg)
end

-- Profiles from `agentbox status --json`, as pick items. Returns nil on failure.
local function profiles()
  local r, err = rex.exec({ opts.agentbox, "status", "--json" }, { timeout = 60 })
  if not r then warn("cannot run agentbox: " .. tostring(err)); return nil end
  if not r.ok then warn("status failed: " .. (r.stderr or "")); return nil end
  local ok, data = pcall(rex.json.decode, r.stdout)
  if not ok or type(data) ~= "table" or type(data.profiles) ~= "table" then
    warn("status --json was not understood"); return nil
  end
  local items = {}
  for _, p in ipairs(data.profiles) do
    if valid_name(p.name) and p.enabled ~= false and p.valid ~= false then
      local job = p.job or {}
      local detail = job.state and job.state ~= "none" and job.state or ""
      if job.progress and job.progress.stalled then detail = "stalled" end
      local vm = type(job.vm) == "string" and job.vm or nil   -- known only once the profile has had a job
      table.insert(items, {
        id = p.name, title = p.name, icon = "package", detail = detail,
        subtitle = "vm " .. tostring(vm or p.vm or "?"),
        running = job.state == "running",
        -- up: the sandbox exists and isn't known to be stopped
        up = p.sandbox_exists == true and (vm == nil or vm == "running"),
      })
    end
  end
  return items
end

local function pick_profile(title, filter)
  local items = profiles()
  if not items then return nil end
  if filter then
    local kept = {}
    for _, it in ipairs(items) do
      if filter(it) then table.insert(kept, it) end
    end
    items = kept
  end
  if #items == 0 then warn(filter and "no profile to pick" or "no enabled profiles"); return nil end
  if type(title) == "function" then title = title(items) end
  local name = rex.ui.pick{ title = title, placeholder = "Filter profiles", items = items }
  if not valid_name(name) then return nil end
  for _, it in ipairs(items) do
    if it.id == name then return name, it end
  end
end

local function term(command, label, keep_open, extra)
  local o = { command = command, shell = command and "none" or nil, cwd = HOME }
  if keep_open then o.exit = { on_completion = false } end
  for k, v in pairs(extra or {}) do o[k] = v end
  local b = rex.layout.block{ flavor = TERMINAL, options = o }
  if label then b.block.label = label end
  return b
end

local function find_session(label)
  local r = rex.call("session.list")
  for _, s in ipairs(r and r.sessions or {}) do
    if s.label == label then return s.session_id end
  end
end

local function show(session_id)
  rex.client.queue("session.select", { session_id = session_id })
end

-- Focus the session with this label, or create it with these windows.
local function ensure_session(label, windows)
  local id = find_session(label)
  if not id then
    local r, err = rex.call("session.create", { label = label, initial_windows = windows })
    if not r then warn("cannot create session " .. label .. ": " .. tostring(err)); return end
    id = r.session_id
  end
  show(id)
  return id
end

local function ab(...)
  return { opts.agentbox, ... }
end

-- The window labelled `label` in a session, if Rex lists it: from session.list's windows, else window.list.
local function find_window(session_id, label)
  local lists = {}
  local r = rex.call("session.list")
  for _, s in ipairs(r and r.sessions or {}) do
    if s.session_id == session_id then table.insert(lists, s.windows) end
  end
  local ok, w = pcall(rex.call, "window.list", { session_id = session_id })
  if ok and type(w) == "table" then table.insert(lists, w.windows) end
  for _, ws in ipairs(lists) do
    for _, win in ipairs(type(ws) == "table" and ws or {}) do
      if win.window_label == label or win.label == label then return win.window_id or win.id end
    end
  end
end

-- Review items per clone-mode profile, from `agentbox review --json` (no git parsing here).
local function review_counts()
  local r, err = rex.exec({ opts.agentbox, "review", "--json" }, { timeout = 60 })
  if not r then warn("cannot run agentbox: " .. tostring(err)); return nil end
  if not r.ok then warn("review --json failed: " .. (r.stderr or "")); return nil end
  local ok, data = pcall(rex.json.decode, r.stdout)
  if not ok or type(data) ~= "table" or type(data.profiles) ~= "table" then
    warn("review --json was not understood"); return nil
  end
  return data.profiles
end

local function count_text(c)
  local parts = {}
  for _, k in ipairs({ { "ready", "ready" }, { "not_fetched", "not fetched" }, { "diverged", "diverged" } }) do
    local n = type(c) == "table" and tonumber(c[k[1]]) or 0
    if n and n > 0 then table.insert(parts, math.floor(n) .. " " .. k[2]) end
  end
  return #parts > 0 and table.concat(parts, " · ") or "nothing to review"
end

function M.setup(user)
  for k, v in pairs(user or {}) do opts[k] = v end

  rex.action{
    name = "agentbox_open", title = "agentbox: Open Profile…", category = "agentbox",
    keywords = "sandbox sbx agent profile",
    run = function(ctx)
      local p, item = pick_profile("Open agentbox profile")
      if not p then return end
      -- The Agent tab doesn't start anything by itself: the command is typed, not run.
      local agent_cmd = (item.running and "agentbox resume " or "agentbox up ") .. p
      ensure_session("ab:" .. p, {
        { window_label = "Agent", layout = term(nil, "agent", false, { initial_input = agent_cmd }) },
        { window_label = "Progress", layout = term(ab("progress", p, "--watch"), "progress", true) },
        { window_label = "Shell", layout = term(nil, "shell") },
      })
    end,
  }

  rex.action{
    name = "agentbox_run", title = "agentbox: Run Job…", category = "agentbox",
    keywords = "sandbox headless job prompt brief",
    run = function(ctx)
      local p = pick_profile("Run a job in")
      if not p then return end
      local path = rex.ui.input{
        title = "Prompt file for " .. p, value = HOME .. "/gits/agentbox-briefs/", confirm = "Run job",
      }
      if not path or path == "" then return end
      path = path:gsub("^~", HOME)
      local f = io.open(path, "r")
      if not f then warn("cannot read prompt file " .. path); return end
      f:close()
      -- Positional args to sh, never spliced into the script: the path is never parsed by a shell.
      local cmd = { "/bin/sh", "-c",
        '"$0" run "$1" --prompt-file "$2" --detach && exec "$0" progress "$1" --watch',
        opts.agentbox, p, path }
      local id = find_session("ab:" .. p)
      if id then
        rex.call("session.new_window", { session_id = id, window_label = "Job", layout = term(cmd, "job", true) })
        show(id)
      else
        ensure_session("ab:" .. p, { { window_label = "Job", layout = term(cmd, "job", true) } })
      end
    end,
  }

  rex.action{
    name = "agentbox_dashboard", title = "agentbox: Dashboard", category = "agentbox",
    keywords = "sandbox overview status",
    run = function(ctx)
      ensure_session("agentbox", {
        { window_label = "Dashboard", layout = term(ab("dashboard", "--interactive"), "dashboard", true) },
      })
    end,
  }

  -- `agentbox <verb> <p>` in a visible tab: in session ab:<p> if there is one, else here.
  local function in_tab(verb, p)
    local layout = term(ab(verb, p), verb, true)
    local id = find_session("ab:" .. p)
    if id then
      rex.call("session.new_window", { session_id = id, window_label = verb, layout = layout })
      show(id)
    else
      rex.session.new_window{ window_label = verb .. " " .. p, layout = layout }
    end
  end

  local function visible(name, title, verb)
    rex.action{
      name = name, title = title, category = "agentbox",
      run = function(ctx)
        local p = pick_profile(title:gsub("…$", ""))
        if not p then return end
        in_tab(verb, p)
      end,
    }
  end
  visible("agentbox_fetch", "agentbox: Fetch for Review…", "fetch")
  visible("agentbox_down", "agentbox: Remove Sandbox…", "down")

  rex.action{
    name = "agentbox_review", title = "agentbox: Review…", category = "agentbox",
    keywords = "merge branch diff pull request fetch",
    run = function(ctx)
      local profs = review_counts()
      if not profs then return end
      local items, total = {}, { ready = 0, not_fetched = 0, diverged = 0 }
      for _, p in ipairs(profs) do
        if valid_name(p.name) then
          local c = type(p.counts) == "table" and p.counts or {}
          for k in pairs(total) do total[k] = total[k] + (tonumber(c[k]) or 0) end
          table.insert(items, { id = p.name, title = p.name, icon = "package",
            subtitle = type(p.error) == "string" and "cannot read the workspace" or count_text(c) })
        end
      end
      if #items == 0 then warn("no clone-mode profiles to review"); return end
      table.insert(items, 1, { id = "*", title = "All profiles", icon = "package", subtitle = count_text(total) })
      local pick = rex.ui.pick{ title = "Review", placeholder = "Filter profiles", items = items }
      if pick ~= "*" and not valid_name(pick) then return end
      local label = pick == "*" and "agentbox" or "ab:" .. pick
      local cmd = pick == "*" and ab("review") or ab("review", pick)
      local window = { window_label = "Review", layout = term(cmd, "review", false) }
      local id = find_session(label)
      if not id then ensure_session(label, { window }); return end
      local win = find_window(id, "Review")
      if win then
        rex.client.queue("window.select", { session_id = id, window_id = win })
      else
        rex.call("session.new_window", { session_id = id, window_label = "Review", layout = window.layout })
      end
      show(id)
    end,
  }

  rex.action{
    name = "agentbox_stop", title = "agentbox: Stop…", category = "agentbox",
    keywords = "sandbox sbx stop halt",
    run = function(ctx)
      local p, item = pick_profile(function(items)
        for _, it in ipairs(items) do
          if it.running then return "Stop sandbox (a running job there will be lost)" end
        end
        return "Stop sandbox"
      end, function(it) return it.up end)
      if not p then return end
      if item.running then
        local typed = rex.ui.input{
          title = "A job is running in " .. p .. ": stopping kills it and it will be reported as lost. "
            .. "Type " .. p .. " to stop it",
          value = "", confirm = "Stop",
        }
        if typed ~= p then
          if typed then warn("stop cancelled: the name did not match") end
          return
        end
      end
      in_tab("stop", p)
    end,
  }
end

return M
