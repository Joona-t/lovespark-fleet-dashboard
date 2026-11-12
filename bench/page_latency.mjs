// Page cost per theme: how long one live update takes to reach the screen in headless Chrome.
//
//   node bench/page_latency.mjs <dashboard-url> [rounds]          rounds default 5
//
// For every Server-Sent Event the page gets: js = render() time, frame = from the event to the second
// requestAnimationFrame after it (style, layout and paint included), net = browser receive time minus the server's
// send stamp (msg.t; the two clocks must agree, see the skew line). Themes alternate inside each round so drift hits
// all of them; each theme collects UPDATES events per round, in the grid and with one machine's full stats open.
import { spawn } from "node:child_process";
import { rmSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const URL = process.argv[2], ROUNDS = +(process.argv[3] || 5), UPDATES = 6;
const THEMES = ["", "cream", "basalt", "kr-seoul", "kr-gingham", "kr-holo", "kr-neon"];
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", PORT = 9300 + process.pid % 600;
const sleep = ms => new Promise(r => setTimeout(r, ms));

const dir = join(tmpdir(), `fleet-latency-${process.pid}`);
const proc = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${dir}`, "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
let ver; for (let i = 0; i < 50 && !ver; i++) { await sleep(200); ver = await fetch(`http://127.0.0.1:${PORT}/json/version`).then(r => r.json()).catch(() => null); }
const ws = new WebSocket(ver.webSocketDebuggerUrl); await new Promise(r => ws.onopen = r);
let id = 0; const wait = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && wait.has(m.id)) { wait.get(m.id)(m); wait.delete(m.id); } };
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; wait.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
const { result: { sessionId } } = await send("Target.attachToTarget", { targetId, flatten: true });
const js = async expr => (await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }, sessionId)).result?.result?.value;
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 2, mobile: false }, sessionId);
await send("Page.navigate", { url: URL }, sessionId);
await sleep(5000);
await js(`window.__m = []; window.__theme = ""; const __orig = onMessage;
  onMessage = msg => { const recv = Date.now() / 1000, t0 = performance.now(); __orig(msg); const t1 = performance.now();
    requestAnimationFrame(() => requestAnimationFrame(() => __m.push({ theme: __theme, view: __view, js: t1 - t0,
      frame: performance.now() - t0, net: recv - msg.t }))); }; window.__view = "grid"; true`);

const res = {};
for (let r = 0; r < ROUNDS; r++)
  for (const view of ["grid", "full"])
    for (const theme of THEMES) {
      await js(`__theme = ${JSON.stringify(theme)}; __view = ${JSON.stringify(view)}; setTheme(__theme);
        S.open = ${view === "full" ? "S.devices[S.devices.length - 1].id" : "null"}; render(); __m = []; true`);
      await js(`document.fonts.ready.then(() => true)`);
      const t = Date.now();
      while ((await js("__m.length")) < UPDATES && Date.now() - t < 20000) await sleep(200);
      for (const m of await js("__m")) ((res[`${view}|${theme || "paper"}`] ??= []).push(m));
    }

const med = a => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
const p95 = a => { const s = [...a].sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(s.length * 0.95))]; };
console.log(`${ROUNDS} rounds, ${UPDATES} updates per theme per round, 1440x1000 @2x, headless Chrome`);
console.log("view  theme        n    js median/p95 ms    frame median/p95 ms    server->browser median ms");
for (const [k, ms] of Object.entries(res)) {
  const [view, theme] = k.split("|"), f = x => ms.map(m => m[x]);
  console.log(`${view.padEnd(5)} ${theme.padEnd(11)} ${String(ms.length).padStart(3)}   ${med(f("js")).toFixed(1).padStart(6)} / ${p95(f("js")).toFixed(1).padStart(5)}`
    + `         ${med(f("frame")).toFixed(1).padStart(6)} / ${p95(f("frame")).toFixed(1).padStart(5)}            ${(med(f("net")) * 1000).toFixed(0).padStart(5)}`);
}
ws.close(); proc.kill(); try { rmSync(dir, { recursive: true, force: true }); } catch {}
process.exit(0);
