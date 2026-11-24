// share/vscode/model.js under bare node (run by tests/test_vscode.py). Prints "ok" or throws.
"use strict";
const assert = require("assert");
const M = require(process.argv[2]);

const now = 1795538881;
const row = (name, state, extra = {}) => ({ name, state, enabled: true, agent: "claude", mode: "clone", vm: "running",
  job: { id: "j1", state: "running", progress: { elapsed_s: 125, stalled: false, tokens: { total: 1500 } } },
  sessions: [], activity_age_s: null, permission_mode: null, review: null, resources: null, ...extra });

// sorting follows the dashboard's groups, then name
const sorted = M.sortRows([row("z", "done"), row("b", "error"), row("a", "working"), row("c", "interactive_working"), row("d", "bogus")]);
assert.deepStrictEqual(sorted.map(r => r.name), ["a", "c", "b", "z", "d"]);
assert.strictEqual(M.stateOf(row("d", "bogus")), "unknown");

// description, review marker, context flags
assert.strictEqual(M.describe(row("a", "working"), now), "working · 2m · claude");
const rv = row("a", "done", { review: { counts: { ready: 1, merged: 2 }, commits: 3 },
  job: { id: "j", state: "finished", exit_code: 0, finished_at: new Date((now - 600) * 1000).toISOString() } });
assert.strictEqual(M.describe(rv, now), "◆ done · 10m ago · claude");
const flags = M.contextFlags(rv);
for (const f of [" profile ", " clone ", " running ", " review-pending ", " has-job ", " enabled "]) assert.ok(flags.includes(f), f);
assert.ok(!flags.includes(" job-running "));
assert.ok(M.contextFlags(row("a", "working")).includes(" job-running "));

// text from the VM or the agent is cleaned
assert.strictEqual(M.clean("a\x1b[2Jb\u202ec\nd"), "a?[2Jb?c d");
assert.strictEqual(M.clean("x".repeat(10), 5), "xxxx…");
const d = M.details(row("a", "working", { job: { id: "j\x07", state: "running", progress: { note: "n\x1b]0;t\x07", plan: { steps: [{ text: "s\x1b", status: "active" }], done: 0, total: 1 } } } }), now);
const all = JSON.stringify(d);
assert.ok(!/[\x00-\x1f]/.test(all.replace(/\\[nrt"\\]/g, "").replace(/\\u00[0-1][0-9a-f]/g, "\x01")), all);
assert.strictEqual(M.cleanLog("a\x1b[31mred\x1b[0m\n\tb\x1b]0;title\x07c\x00"), "ared\n\tbc");

// parseDashboard keeps only rows with a valid name; rejects non-dashboards
const parsed = M.parseDashboard(JSON.stringify({ sandboxes_known: true, profiles: [{ name: "ok-1", state: "done" }, { name: "Bad Name", state: "done" }, { name: "-x" }, null, { name: "a;rm" }] }));
assert.deepStrictEqual(parsed.profiles.map(r => r.name), ["ok-1"]);
assert.throws(() => M.parseDashboard("[]"));
assert.throws(() => M.parseDashboard("nope"));
assert.throws(() => M.parseDashboard(JSON.stringify({ profiles: [{ name: "a" }] })), /older/);   // an agentbox without `state`

// transitions: none on first load; a job that ended, once; a profile that became stalled
const fin = (id, code) => ({ job: { id, state: "finished", exit_code: code } });
const before = M.stateMap({ profiles: [row("a", "working"), row("b", "working"), row("c", "nojob", { job: null }),
  row("s", "working")] });
const after = { profiles: [row("a", "done", fin("j1", 0)), row("b", "working"), row("c", "interactive_working", { job: null }),
  row("n", "error", fin("j1", 1)), row("s", "stalled")] };
assert.deepStrictEqual(M.transitions(null, after), []);
assert.deepStrictEqual(M.transitions(before, after), [{ name: "a", state: "done" }, { name: "s", state: "stalled" }]);
// an interactive session closing drops back to an old job's outcome: no repeat
const old = fin("j0", 0);
const inter = M.stateMap({ profiles: [row("a", "interactive_idle", old), row("b", "interactive_idle", { job: { id: "j0", state: "lost" } })] });
assert.deepStrictEqual(M.transitions(inter, { profiles: [row("a", "done", old), row("b", "lost", { job: { id: "j0", state: "lost" } })] }), []);
// a job that finished during an interactive session is reported, with its own outcome
const busy = M.stateMap({ profiles: [row("a", "interactive_working")] });
assert.deepStrictEqual(M.transitions(busy, { profiles: [row("a", "interactive_idle", fin("j1", 1))] }), [{ name: "a", state: "error" }]);
// a new job that started and ended between two polls
assert.deepStrictEqual(M.transitions(M.stateMap({ profiles: [row("a", "done", old)] }), { profiles: [row("a", "error", fin("j2", 3))] }),
  [{ name: "a", state: "error" }]);
// and the next poll is quiet
assert.deepStrictEqual(M.transitions(M.stateMap(after), after), []);

// status bar
const s = M.summary({ sandboxes_known: true, profiles: [row("a", "working"), row("b", "stalled"), rv] });
assert.strictEqual(s.text, "agentbox $(sync~spin) 1 $(warning) 1 $(git-pull-request) 1");
assert.strictEqual(s.attention, true);
assert.strictEqual(M.summary({ sandboxes_known: true, profiles: [] }).text, "agentbox");

assert.strictEqual(M.fmtDur(3725), "1h02m");
assert.strictEqual(M.fmtBytes(1536 * 1024 * 1024), "1.5G");
assert.strictEqual(M.fmtCount(4757052), "4.8M");
console.log("ok");
