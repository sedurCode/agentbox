// Copyright (c) 2026 SEDAudio Ltd.
// SPDX-License-Identifier: AGPL-3.0-only OR LicenseRef-SEDAudio-Commercial
// See LICENSING.md.
//
// Plain functions over `agentbox dashboard --json`: no `vscode` import, so tests run under bare node.
// Every string that came from agentbox goes through clean() before it is shown.
"use strict";

const NAME_RE = /^[a-z0-9][a-z0-9-]{0,40}$/;   // agentbox's NAME_RE: the only names ever passed as an argument

// display state (the row's `state`, from agentbox's dash_state) -> icon, theme colour, label, sort group
const STATES = {
  working:             { icon: "sync~spin",        color: "charts.green",  label: "working",           group: 0 },
  interactive_working: { icon: "record",           color: "charts.blue",   label: "interactive",       group: 1 },
  stalled:             { icon: "warning",          color: "charts.yellow", label: "stalled",           group: 2 },
  error:               { icon: "error",            color: "charts.red",    label: "failed",            group: 2 },
  lost:                { icon: "debug-disconnect", color: "charts.red",    label: "lost",              group: 2 },
  interactive_idle:    { icon: "circle-outline",   color: "charts.blue",   label: "interactive, idle", group: 3 },
  done:                { icon: "pass",             color: "charts.green",  label: "done",              group: 4 },
  nojob:               { icon: "vm-running",       color: null,            label: "no job",            group: 5 },
  stopped:             { icon: "vm-outline",       color: "disabledForeground", label: "stopped",      group: 5 },
  disabled:            { icon: "circle-slash",     color: "disabledForeground", label: "disabled",     group: 5 },
  unknown:             { icon: "question",         color: "charts.yellow", label: "unknown",           group: 5 },
};
const ATTENTION = new Set(["stalled", "error", "lost"]);
const NOTIFY = new Set(["done", "error", "stalled", "lost"]);   // states worth a notification when entered

/** Control, format and bidi characters out, at most `max` characters. */
function clean(s, max = 200) {
  if (s === null || s === undefined) return "";
  const t = String(s).replace(/[\t\n\r]/g, " ").replace(/[\p{Cc}\p{Cf}\p{Co}\p{Cn}\p{Zl}\p{Zp}]/gu, "?");
  return t.length > max ? t.slice(0, max - 1) + "…" : t;
}

function stateOf(row) {
  return row && Object.prototype.hasOwnProperty.call(STATES, row.state) ? row.state : "unknown";
}

function isInt(v) { return Number.isInteger(v) && v >= 0; }

function fmtDur(s) {
  if (!isInt(s)) return "-";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 86400)}d${Math.floor(s % 86400 / 3600)}h`;
}

function fmtBytes(n) {
  if (typeof n !== "number" || !isFinite(n)) return "?";
  for (const unit of ["B", "K", "M", "G", "T"]) {
    if (n < 1024 || unit === "T") return (unit === "B" || unit === "K" || n >= 100) ? `${n.toFixed(0)}${unit}` : `${n.toFixed(1)}${unit}`;
    n /= 1024;
  }
  return "?";
}

function fmtCount(n) {
  if (!isInt(n)) return "-";
  for (const [div, unit] of [[1e9, "G"], [1e6, "M"], [1e3, "k"]]) if (n >= div) return `${(n / div).toFixed(1)}${unit}`;
  return String(n);
}

function pct(used, total) { return typeof used === "number" && total > 0 ? Math.round(100 * used / total) : null; }

/** Seconds since an ISO time, or null. */
function since(iso, nowS) {
  const t = typeof iso === "string" ? Date.parse(iso) : NaN;
  return isNaN(t) ? null : Math.max(0, Math.floor(nowS - t / 1000));
}

function reviewPending(row) {
  const c = (row.review && row.review.counts) || {};
  return (c.ready || 0) + (c.not_fetched || 0) + (c.diverged || 0) > 0;
}

function sortRows(rows) {
  return [...rows].sort((a, b) => STATES[stateOf(a)].group - STATES[stateOf(b)].group ||
    String(a.name).localeCompare(String(b.name)));
}

/** The short "when" beside a profile: elapsed for a working job, activity for sessions, age of a finished job. */
function when(row, nowS) {
  const st = stateOf(row), job = row.job || {}, p = job.progress || {};
  if (st === "working" || st === "stalled") return fmtDur(p.elapsed_s);
  if (st === "interactive_working") return isInt(row.activity_age_s) ? `${fmtDur(row.activity_age_s)} ago` : "";
  if (st === "interactive_idle") return isInt(row.activity_age_s) ? `idle ${fmtDur(row.activity_age_s)}` : "";
  if (["done", "error", "lost"].includes(st)) {
    const ago = since(job.finished_at, nowS);
    return ago === null ? "" : `${fmtDur(ago)} ago`;
  }
  return "";
}

/** description shown beside the name in the tree */
function describe(row, nowS) {
  const parts = [STATES[stateOf(row)].label];
  const w = when(row, nowS);
  if (w) parts.push(w);
  if (row.agent) parts.push(clean(row.agent, 20));
  return (reviewPending(row) ? "◆ " : "") + parts.join(" · ");
}

/** contextValue: a space-separated set of flags the menus' `when` clauses match with =~ */
function contextFlags(row) {
  const f = ["profile", `state-${stateOf(row)}`];
  if (row.enabled !== false) f.push("enabled");
  if (row.vm === "running") f.push("running");
  if (row.mode === "clone") f.push("clone");
  if (reviewPending(row)) f.push("review-pending");
  const job = row.job || {};
  if (job.state && job.state !== "none") f.push("has-job");
  if (job.state === "running") f.push("job-running");
  return " " + f.join(" ") + " ";
}

/** Detail rows under a profile: [{label, description?, icon?, children?}] */
function details(row, nowS) {
  const out = [];
  const res = row.resources;
  if (row.vm === "running") {
    if (res) {
      out.push({ label: "VM", description: `up ${fmtDur(res.uptime_s)} · ${res.cpus} cpu · load ${(res.load || []).map(x => Number(x).toFixed(2)).join(" ")}`, icon: "vm-running" });
      out.push({ label: "cpu", description: res.cpu_pct === null ? "?" : `${Math.round(res.cpu_pct)}%`, icon: "pulse" });
      out.push({ label: "memory", description: `${pct(res.mem_used, res.mem_total) ?? "?"}% · ${fmtBytes(res.mem_used)}/${fmtBytes(res.mem_total)}`, icon: "server" });
      out.push({ label: "disk", description: `${pct(res.disk_used, res.disk_total) ?? "?"}% · ${fmtBytes(res.disk_used)}/${fmtBytes(res.disk_total)}`, icon: "database" });
    } else {
      out.push({ label: "VM", description: "running, but could not be read", icon: "warning" });
    }
  } else {
    out.push({ label: "VM", description: clean(row.vm || "unknown", 20), icon: "vm-outline" });
  }
  const sessions = Array.isArray(row.sessions) ? row.sessions : [];
  if (sessions.length) {
    const act = isInt(row.activity_age_s) ? `active ${fmtDur(row.activity_age_s)} ago` : "no activity seen";
    const mode = typeof row.permission_mode === "string" ? ` · mode ${clean(row.permission_mode, 20)}` : "";
    out.push({ label: "interactive", description: `${sessions.length} session${sessions.length === 1 ? "" : "s"} · ${act}${mode}`,
      icon: row.permission_mode === "bypassPermissions" ? "shield" : "terminal",
      children: sessions.map(s => ({ label: clean(s.agent, 20), description: `running ${fmtDur(s.age_s)}`, icon: "terminal" })) });
  }
  const job = row.job || {};
  if (job.state && job.state !== "none") {
    const p = job.progress;
    const state = job.state === "finished" ? `finished, exit ${job.exit_code ?? "?"}` : clean(job.state, 20);
    const kids = [];
    if (p) {
      kids.push({ label: "elapsed", description: fmtDur(p.elapsed_s) + (isInt(p.last_event_age_s) ? ` · idle ${fmtDur(p.last_event_age_s)}` : "") });
      kids.push({ label: "turns", description: `${p.turns ?? "-"} · tools ${p.tool_calls ?? "-"}` +
        (Array.isArray(p.tools_top) && p.tools_top.length ? ` (${p.tools_top.slice(0, 3).map(t => `${clean(t.name, 30)} ${t.count}`).join(", ")})` : "") });
      const t = p.tokens || {};
      const cost = typeof p.cost_usd === "number" ? ` · ${p.cost_is_estimate ? "~" : ""}$${p.cost_usd.toFixed(2)}` : "";
      kids.push({ label: "tokens", description: `${fmtCount(t.total)} (in ${fmtCount(t.input)} out ${fmtCount(t.output)} cache ${fmtCount((t.cache_read || 0) + (t.cache_creation || 0))})${cost}` });
      if (isInt(p.commits)) kids.push({ label: "commits", description: String(p.commits) + (p.branch ? ` on ${clean(p.branch, 60)}` : "") });
      if (isInt(p.denials) && p.denials > 0) kids.push({ label: "denied", description: String(p.denials), icon: "warning" });
      if (p.stalled) kids.push({ label: "stalled", description: "no events for a while", icon: "warning" });
      const plan = p.plan;
      if (plan && Array.isArray(plan.steps) && plan.steps.length) {
        kids.push({ label: "plan", description: `${plan.done ?? 0}/${plan.total ?? plan.steps.length} (the agent's own claim)`, icon: "checklist",
          children: plan.steps.slice(0, 50).map(s => ({ label: clean(s.text, 120),
            icon: s.status === "done" ? "pass" : s.status === "active" ? "play" : "circle-large-outline" })) });
      }
      if (p.note) kids.push({ label: "note", description: clean(p.note, 200), icon: "note" });
    }
    if (job.started_at) kids.unshift({ label: "started", description: clean(job.started_at, 30) });
    out.push({ label: "job", description: `${clean(job.id, 40)} · ${state}`, icon: "tasklist", children: kids, expanded: job.state === "running" });
  } else {
    out.push({ label: "job", description: "none", icon: "tasklist" });
  }
  if (row.review) {
    const c = row.review.counts || {};
    const n = row.review.commits || 0;
    const bits = Object.entries(c).filter(([, v]) => v).map(([k, v]) => `${v} ${k.replace("_", " ")}`);
    out.push({ label: "review", description: (n ? `${n} commit${n === 1 ? "" : "s"} ready · ` : "") + (bits.join(" · ") || "nothing"),
      icon: reviewPending(row) ? "git-pull-request" : "git-merge" });
  }
  return out;
}

/** Status bar text and whether it needs attention. */
function summary(data) {
  if (!data) return { text: "agentbox", attention: false };
  const rows = data.profiles || [];
  const count = pred => rows.filter(pred).length;
  const working = count(r => stateOf(r) === "working");
  const inter = count(r => stateOf(r).startsWith("interactive"));
  const attn = count(r => ATTENTION.has(stateOf(r)));
  const review = count(reviewPending);
  const parts = [];
  if (working) parts.push(`$(sync~spin) ${working}`);
  if (inter) parts.push(`$(terminal) ${inter}`);
  if (attn) parts.push(`$(warning) ${attn}`);
  if (review) parts.push(`$(git-pull-request) ${review}`);
  if (!data.sandboxes_known) parts.push("$(error) sbx");
  return { text: "agentbox" + (parts.length ? " " + parts.join(" ") : ""), attention: attn > 0 || !data.sandboxes_known };
}

/** How a row's job ended: done | error | lost, or null while it runs or with no job. Read from the job itself,
 * not the display state, which shows an interactive session ahead of the job's outcome. */
function jobOutcome(row) {
  const job = (row && row.job) || {};
  if (job.state === "finished") return job.exit_code === 0 ? "done" : "error";
  return job.state === "lost" ? "lost" : null;
}

function jobId(row) {
  return row && row.job && typeof row.job.id === "string" ? row.job.id : null;
}

/** Notifications since `prev` (from stateMap), not on the first load: a job that ended (done, error or lost),
 * once, when it was seen running or is a job not seen before; and a profile that became stalled. A display
 * state falling back to an old job's outcome (an interactive session closing) is not news. */
function transitions(prev, data) {
  const out = [];
  if (!prev || !data) return out;
  for (const r of data.profiles || []) {
    const before = prev.get(r.name);
    if (before === undefined || !NAME_RE.test(r.name)) continue;
    const st = stateOf(r);
    const outcome = jobOutcome(r);
    if (outcome && NOTIFY.has(outcome) && (before.running || before.job !== jobId(r))) out.push({ name: r.name, state: outcome });
    else if (st === "stalled" && before.state !== "stalled") out.push({ name: r.name, state: st });
  }
  return out;
}

/** name -> { state, job id, whether that job was running }: what transitions() compares against. */
function stateMap(data) {
  return new Map(((data && data.profiles) || []).map(r =>
    [r.name, { state: stateOf(r), job: jobId(r), running: Boolean(r.job && r.job.state === "running") }]));
}

/** The dashboard document, or throws: a JSON object with a profiles array of rows with safe names. */
function parseDashboard(text) {
  const d = JSON.parse(text);
  if (!d || typeof d !== "object" || !Array.isArray(d.profiles)) throw new Error("not a dashboard document");
  if (d.profiles.length && !d.profiles.some(r => r && typeof r.state === "string"))
    throw new Error("this agentbox is older than the VS Code extension (no `state` in dashboard --json): run install.sh again");
  d.profiles = d.profiles.filter(r => r && typeof r === "object" && typeof r.name === "string" && NAME_RE.test(r.name));
  return d;
}

/** ANSI escapes and other controls out of a log, newlines kept. */
function cleanLog(text) {
  return String(text).replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "")
    .replace(/[\p{Cc}\p{Cf}]/gu, ch => ch === "\n" || ch === "\t" ? ch : "");
}

module.exports = { NAME_RE, STATES, clean, cleanLog, stateOf, fmtDur, fmtBytes, fmtCount, describe, details, contextFlags,
  sortRows, summary, transitions, stateMap, parseDashboard, reviewPending };
