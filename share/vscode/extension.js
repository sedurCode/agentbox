// Copyright (c) 2026 SEDAudio Ltd.
// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
// See LICENSING.md.
//
// agentbox in VS Code: the interactive dashboard as a tree view, a status bar summary and notifications.
// It reads `agentbox dashboard --json` and runs only progress --watch, logs, fetch, up, stop, review and code,
// each with only a profile name that matches agentbox's NAME_RE. Never --force, --start, down, fresh, run
// or resume. Processes are started without a shell, so nothing is parsed by one.
"use strict";

const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");
const M = require("./model");

const LOG_SCHEME = "agentbox-log";

function cfg() { return vscode.workspace.getConfiguration("agentbox"); }

/** The agentbox CLI: the setting, else ~/.local/bin/agentbox(.cmd) if present (a GUI app's PATH often lacks
 * ~/.local/bin), else `agentbox` on PATH. */
function agentboxPath() {
  const set = cfg().get("path");
  if (set) return set.startsWith("~") ? path.join(os.homedir(), set.slice(1)) : set;
  const local = path.join(os.homedir(), ".local", "bin", process.platform === "win32" ? "agentbox.cmd" : "agentbox");
  return fs.existsSync(local) ? local : "agentbox";
}

/** Run agentbox and collect stdout. A .cmd needs cmd.exe on Windows, and with `shell` node joins the command
 * line with spaces and quotes nothing, so the path is quoted here (C:\Users\John Smith\...); the arguments are
 * fixed words and a NAME_RE-checked profile name, so nothing in them is special to cmd.exe. */
function runAgentbox(args, timeoutMs) {
  const exe = agentboxPath();
  const shell = process.platform === "win32" && /\.(cmd|bat)$/i.test(exe);
  return new Promise((resolve, reject) => {
    cp.execFile(shell ? `"${exe}"` : exe, args, { timeout: timeoutMs, maxBuffer: 64 << 20, shell, windowsHide: true,
      env: { ...process.env, AGENTBOX_NO_OSC: "1", NO_COLOR: "1" } },
    (err, stdout, stderr) => err ? reject(Object.assign(err, { stderr: String(stderr || "") })) : resolve(String(stdout)));
  });
}

function checkName(name) {
  if (typeof name !== "string" || !M.NAME_RE.test(name)) throw new Error(`not a profile name: ${M.clean(name, 60)}`);
  return name;
}

// ------------------------------------------------------------------ the tree
class Provider {
  constructor() {
    this.data = null;
    this.error = null;
    this.emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this.emitter.event;
  }

  set(data, error) {
    this.data = data;
    this.error = error;
    this.emitter.fire();
  }

  row(name) { return ((this.data && this.data.profiles) || []).find(r => r.name === name); }

  getTreeItem(el) { return el.item; }

  getChildren(el) {
    if (el) return (el.children || []).map(d => this.detailNode(d));
    if (this.error && !this.data) {
      const it = new vscode.TreeItem("agentbox could not be read");
      it.description = M.clean(this.error, 200);
      it.tooltip = M.clean(this.error, 2000);
      it.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("errorForeground"));
      it.command = { command: "workbench.action.openSettings", title: "Settings", arguments: ["agentbox.path"] };
      return [{ item: it }];
    }
    if (!this.data) return [];
    const now = Math.floor(Date.now() / 1000);
    const out = [];
    if (!this.data.sandboxes_known) {
      const it = new vscode.TreeItem("could not list sandboxes (sbx ls --json failed)");
      it.iconPath = new vscode.ThemeIcon("error", new vscode.ThemeColor("errorForeground"));
      out.push({ item: it });
    }
    for (const row of M.sortRows(this.data.profiles)) out.push(this.profileNode(row, now));
    return out;
  }

  profileNode(row, now) {
    const st = M.STATES[M.stateOf(row)];
    const it = new vscode.TreeItem(row.name, vscode.TreeItemCollapsibleState.Collapsed);
    it.id = `profile:${row.name}`;
    it.description = M.describe(row, now);
    it.iconPath = new vscode.ThemeIcon(st.icon, st.color ? new vscode.ThemeColor(st.color) : undefined);
    it.contextValue = M.contextFlags(row);
    const md = new vscode.MarkdownString(undefined, false);      // no command links, no HTML
    md.isTrusted = false;
    md.supportHtml = false;
    md.appendMarkdown(`**${row.name}** · `).appendText(`${st.label} · ${M.clean(row.agent, 20)} · ${M.clean(row.mode, 20)} · sandbox ${M.clean(row.sandbox, 60)}`);
    it.tooltip = md;
    return { item: it, name: row.name, children: M.details(row, now) };
  }

  detailNode(d) {
    const it = new vscode.TreeItem(d.label, d.children && d.children.length
      ? (d.expanded ? vscode.TreeItemCollapsibleState.Expanded : vscode.TreeItemCollapsibleState.Collapsed)
      : vscode.TreeItemCollapsibleState.None);
    it.description = d.description;
    it.tooltip = d.description ? `${d.label}: ${d.description}` : d.label;
    if (d.icon) it.iconPath = new vscode.ThemeIcon(d.icon);
    it.contextValue = "detail";
    return { item: it, children: d.children };
  }
}

// ------------------------------------------------------------- the poller
class Poller {
  constructor(provider, onData) {
    this.provider = provider;
    this.onData = onData;
    this.busy = false;
    this.timer = null;
    this.visible = false;
  }

  interval() {
    const fg = Math.max(2, cfg().get("refreshInterval") || 5);
    const bg = cfg().get("backgroundInterval");
    return this.visible ? fg : (bg > 0 ? Math.max(fg, bg) : 0);
  }

  schedule() {
    clearTimeout(this.timer);
    if (this.disposed) return;
    const s = this.interval();
    if (s > 0) this.timer = setTimeout(() => this.refresh(), s * 1000);
  }

  async refresh() {
    if (this.busy || this.disposed) return;
    this.busy = true;
    clearTimeout(this.timer);
    try {
      const out = await runAgentbox(["dashboard", "--json"], 120000);
      const data = M.parseDashboard(out);
      if (this.disposed) return;           // deactivated while agentbox ran: the emitter and status bar are gone
      this.provider.set(data, null);
      this.onData(data, null);
    } catch (e) {
      if (this.disposed) return;
      const msg = e.code === "ENOENT" ? `agentbox not found (${agentboxPath()}); set agentbox.path`
        : (e.stderr && e.stderr.trim()) || e.message;
      this.provider.set(this.provider.data, msg);
      this.onData(null, msg);
    } finally {
      this.busy = false;
      this.schedule();
    }
  }

  setVisible(v) {
    const was = this.visible;
    this.visible = v;
    if (v && !was) this.refresh(); else this.schedule();
  }

  dispose() { this.disposed = true; clearTimeout(this.timer); }
}

// ---------------------------------------------------------------- actions
const terminals = new Map();     // "<verb> <profile>" -> Terminal, reused while open

/** An interactive command in its own terminal (closed when the command exits). */
function inTerminal(title, args, key) {
  const old = terminals.get(key);
  if (old && old.exitStatus === undefined) { old.show(); return; }
  const exe = agentboxPath();
  const win = process.platform === "win32";
  const term = vscode.window.createTerminal({ name: title, isTransient: true,
    shellPath: win ? (process.env.ComSpec || "cmd.exe") : exe,
    shellArgs: win ? ["/d", "/c", exe, ...args] : args,
    iconPath: new vscode.ThemeIcon("server-environment") });
  terminals.set(key, term);
  term.show();
}

/** A one-shot command as a task, so its output stays after it ends. */
function asTask(title, args) {
  const exe = agentboxPath();
  const win = process.platform === "win32";
  const ex = win ? new vscode.ProcessExecution(process.env.ComSpec || "cmd.exe", ["/d", "/c", exe, ...args])
    : new vscode.ProcessExecution(exe, args);
  const task = new vscode.Task({ type: "agentbox", args }, vscode.TaskScope.Global, title, "agentbox", ex, []);
  task.presentationOptions = { reveal: vscode.TaskRevealKind.Always, panel: vscode.TaskPanelKind.New, clear: true };
  return vscode.tasks.executeTask(task);
}

function nameOf(arg, provider) {
  if (arg && typeof arg.name === "string") return checkName(arg.name);
  if (typeof arg === "string") return checkName(arg);
  return null;
}

async function pickProfile(provider, filter, placeHolder) {
  const rows = M.sortRows(((provider.data && provider.data.profiles) || []).filter(filter));
  if (!rows.length) { vscode.window.showInformationMessage(`agentbox: no profile to ${placeHolder}`); return null; }
  const now = Math.floor(Date.now() / 1000);
  const pick = await vscode.window.showQuickPick(rows.map(r => ({ label: r.name, description: M.describe(r, now) })),
    { placeHolder: `profile to ${placeHolder}` });
  return pick ? checkName(pick.label) : null;
}

function register(ctx, provider, poller, logs) {
  const needs = (verb, filter, fn) => async arg => {
    try {
      const name = nameOf(arg, provider) || await pickProfile(provider, filter, verb);
      if (name) await fn(name, provider.row(name) || { name });
    } catch (e) {
      vscode.window.showErrorMessage(`agentbox: ${M.clean(e.message, 500)}`);
    }
  };
  const enabled = r => r.enabled !== false;
  const running = r => r.vm === "running";
  const hasJob = r => r.job && r.job.state && r.job.state !== "none";
  const cmd = (id, fn) => ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));

  cmd("agentbox.refresh", () => poller.refresh());
  cmd("agentbox.dashboardTerminal", () => inTerminal("agentbox dashboard", ["dashboard", "--interactive"], "dashboard"));

  cmd("agentbox.progress", needs("watch", hasJob, name =>
    inTerminal(`progress: ${name}`, ["progress", name, "--watch"], `progress ${name}`)));

  cmd("agentbox.logs", needs("show the log of", hasJob, async name => {
    const uri = vscode.Uri.from({ scheme: LOG_SCHEME, path: `/${name}.log`, query: String(Date.now()) });
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc, { preview: true });
  }));

  cmd("agentbox.fetch", needs("fetch", r => enabled(r) && r.mode === "clone", async (name, row) => {
    if (row.mode !== "clone") { vscode.window.showInformationMessage(`agentbox: ${name} isn't a clone-mode profile`); return; }
    if (row.vm !== "running") {
      vscode.window.showWarningMessage(`agentbox: ${name}'s sandbox is ${M.clean(row.vm || "not running", 20)}. ` +
        "fetch needs it running, and booting it would relaunch the agent.");
      return;
    }
    const job = row.job || {};
    const ref = (job.progress && job.progress.branch) || job.branch || "main";
    const go = await vscode.window.showInformationMessage(`agentbox fetch ${name}`,
      { modal: true, detail: `Fetches ${M.clean(ref, 80)} into the review branch review/${name}. Never merges.` }, "Fetch");
    if (go === "Fetch") await asTask(`fetch ${name}`, ["fetch", name]);
  }));

  cmd("agentbox.up", needs("open", enabled, async (name, row) => {
    if (row.enabled === false) { vscode.window.showWarningMessage(`agentbox: ${name} is disabled`); return; }
    if (row.job && row.job.state === "running") {
      const go = await vscode.window.showWarningMessage(`A job is running in ${name}.`,
        { modal: true, detail: "`agentbox up` attaches a session to the same sandbox." }, "Open anyway");
      if (go !== "Open anyway") return;
    }
    inTerminal(`agent: ${name}`, ["up", name], `up ${name}`);
  }));

  cmd("agentbox.stop", needs("stop", running, async (name, row) => {
    if (row.vm !== "running") { vscode.window.showInformationMessage(`agentbox: ${name} isn't running`); return; }
    if (row.job && row.job.state === "running") {
      const typed = await vscode.window.showInputBox({ title: `Stop ${name}`, ignoreFocusOut: true,
        prompt: `A job is running in ${name}: stopping kills it and it is reported as lost. Type the profile name to stop.`,
        validateInput: v => v === name || v === "" ? null : "type the profile name exactly" });
      if (typed !== name) return;
    } else {
      const go = await vscode.window.showWarningMessage(`agentbox stop ${name}`, { modal: true }, "Stop");
      if (go !== "Stop") return;
    }
    await asTask(`stop ${name}`, ["stop", name]);
  }));

  cmd("agentbox.review", needs("review", r => r.mode === "clone", name =>
    inTerminal(`review: ${name}`, ["review", name], `review ${name}`)));

  cmd("agentbox.code", needs("open in VS Code", r => enabled(r), name => asTask(`code ${name}`, ["code", name])));

  ctx.subscriptions.push(vscode.workspace.registerTextDocumentContentProvider(LOG_SCHEME, logs));
}

class LogProvider {
  async provideTextDocumentContent(uri) {
    const name = checkName(path.posix.basename(uri.path, ".log"));
    try {
      return M.cleanLog(await runAgentbox(["logs", name], 120000));
    } catch (e) {
      return `agentbox logs ${name} failed:\n${M.cleanLog((e.stderr || "").trim() || e.message)}`;
    }
  }
}

// --------------------------------------------------------------- activate
function activate(ctx) {
  const provider = new Provider();
  const status = vscode.window.createStatusBarItem("agentbox.status", vscode.StatusBarAlignment.Left, 50);
  status.name = "agentbox";
  status.command = "agentbox.profiles.focus";
  let prev = null;

  const onData = (data, error) => {
    if (error) {
      status.text = "$(error) agentbox";
      status.tooltip = M.clean(error, 500);
      status.backgroundColor = new vscode.ThemeColor("statusBarItem.errorBackground");
    } else {
      const s = M.summary(data);
      status.text = s.text;
      status.tooltip = "agentbox: working · interactive · needs attention · to review";
      status.backgroundColor = s.attention ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
      if (cfg().get("notifications")) notify(provider, M.transitions(prev, data));
      prev = M.stateMap(data);
    }
    if (cfg().get("statusBar")) status.show(); else status.hide();
  };

  const poller = new Poller(provider, onData);
  const view = vscode.window.createTreeView("agentbox.profiles", { treeDataProvider: provider, showCollapseAll: true });
  view.onDidChangeVisibility(e => poller.setVisible(e.visible), null, ctx.subscriptions);
  ctx.subscriptions.push(view, status, poller, provider.emitter,
    vscode.workspace.onDidChangeConfiguration(e => { if (e.affectsConfiguration("agentbox")) poller.refresh(); }),
    vscode.window.onDidCloseTerminal(t => { for (const [k, v] of terminals) if (v === t) terminals.delete(k); }));
  register(ctx, provider, poller, new LogProvider());
  poller.setVisible(view.visible);
  if (!view.visible) poller.refresh();          // status bar and notifications start without the view
}

async function notify(provider, events) {
  for (const { name, state } of events) {
    const label = M.STATES[state].label;
    const show = state === "done" ? vscode.window.showInformationMessage : vscode.window.showWarningMessage;
    const row = provider.row(name) || {};
    const actions = state === "done" ? (row.mode === "clone" ? ["Logs", "Fetch"] : ["Logs"]) : ["Logs", "Progress"];
    show(`agentbox: ${name} ${label}`, ...actions).then(a => {
      if (a === "Logs") vscode.commands.executeCommand("agentbox.logs", name);
      else if (a === "Fetch") vscode.commands.executeCommand("agentbox.fetch", name);
      else if (a === "Progress") vscode.commands.executeCommand("agentbox.progress", name);
    });
  }
}

function deactivate() {}

module.exports = { activate, deactivate };
