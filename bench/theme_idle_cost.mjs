// What a theme costs a viewer's browser while the page simply stays open (live updates and animations included).
//
//   node bench/theme_idle_cost.mjs <dashboard-url> [rounds] [seconds]        defaults: 5 rounds, 15 s
//
// Chrome's own counters (Performance.getMetrics): TaskDuration = main-thread busy time, ScriptDuration = JS.
// Themes alternate within each round; reported as ms of main-thread time per second of wall time (= 0.1% of a core).
import { spawn } from "node:child_process";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const URL = process.argv[2], ROUNDS = +(process.argv[3] || 5), SECS = +(process.argv[4] || 15);
const THEMES = (process.env.THEMES || "paper,basalt,glyph-basalt,cream,glyph-cream").split(",");
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", PORT = 9300 + process.pid % 600;
const sleep = ms => new Promise(r => setTimeout(r, ms));
const dir = join(tmpdir(), `fleet-idle-${process.pid}`);
const proc = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${dir}`, "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
let ver; for (let i = 0; i < 50 && !ver; i++) { await sleep(200); ver = await fetch(`http://127.0.0.1:${PORT}/json/version`).then(r => r.json()).catch(() => null); }
const ws = new WebSocket(ver.webSocketDebuggerUrl); await new Promise(r => ws.onopen = r);
let id = 0; const wait = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && wait.has(m.id)) { wait.get(m.id)(m); wait.delete(m.id); } };
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; wait.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
const { result: { sessionId } } = await send("Target.attachToTarget", { targetId, flatten: true });
const js = expr => send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }, sessionId);
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 2, mobile: false }, sessionId);
await send("Performance.enable", {}, sessionId);
await send("Page.navigate", { url: URL }, sessionId);
await sleep(5000);
const metrics = async () => Object.fromEntries((await send("Performance.getMetrics", {}, sessionId)).result.metrics.map(m => [m.name, m.value]));
const res = {};
for (let r = 0; r < ROUNDS; r++)
  for (const t of THEMES) {
    await js(`setTheme(${JSON.stringify(t === "paper" ? "" : t)}); S.open = null; render(); true`);
    await sleep(2000);                                            // let first loads and draws settle
    const a = await metrics(), t0 = Date.now(); await sleep(SECS * 1000); const b = await metrics(), dt = (Date.now() - t0) / 1000;
    (res[t] ??= []).push({ task: (b.TaskDuration - a.TaskDuration) / dt * 1000, script: (b.ScriptDuration - a.ScriptDuration) / dt * 1000 });
  }
const med = a => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
console.log(`${ROUNDS} rounds x ${SECS} s per theme, 1440x1000 @2x, headless Chrome; ms of main-thread time per second`);
for (const [t, rs] of Object.entries(res)) {
  const task = rs.map(x => x.task), sc = rs.map(x => x.script);
  console.log(`${t.padEnd(13)} task ${med(task).toFixed(1).padStart(5)} [${Math.min(...task).toFixed(1)}-${Math.max(...task).toFixed(1)}]   script ${med(sc).toFixed(1).padStart(5)}`);
}
ws.close(); proc.kill(); try { rmSync(dir, { recursive: true, force: true }); } catch {}
process.exit(0);
