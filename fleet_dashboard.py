"""Fleet dashboard: every machine on one live page (CPU, memory, GPUs, VRAM, temperatures, power, disks, processes).

    python3 fleet_dashboard.py [--here ID] [--port 8766] [--host 127.0.0.1]

Data flow:

    agent on each machine  --one JSON line per tick-->  one reader thread per machine  -->  Fleet (state + history)
    agent_mac.py   (the Mac named by --here: local process; other Macs: over SSH or pushed)  |-- /events     Server-Sent Events
    agent_win.ps1  (Windows PCs: piped over SSH into PowerShell)                             |-- /api/state  JSON snapshot
    agent_linux.py (Linux PCs: piped over SSH into python3)                                  `-- history/<date>.jsonl (1 line/min)

Nothing is installed on the machines read over SSH: each agent is piped over SSH, lives only as long as its SSH
session and exits by itself after MAX_LIFE seconds, when the server reconnects. A laptop has no SSH server and is
not always there, so it pushes instead: push_mac.sh on the laptop writes its agent's lines into a named pipe here.
Standard library only.
To add a machine: one entry in DEVICES (an SSH alias from ~/.ssh/config, or a list of aliases to try in turn, and
the agent kind, or "auto"; "push_from": <id> for a machine whose readings that machine pushes, when the server
runs anywhere else).
"""
from __future__ import annotations

import argparse
import collections
import ipaddress
import json
import math
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAGE = HERE / "fleet_dashboard.html"
FONTS = {"cinzel.woff2", "ebgaramond.woff2", "lexend.woff2"}   # theme fonts (SIL OFL, fonts/OFL.txt); nothing else is served

PUSH = HERE / "push"                 # one named pipe per pushed device: push/<id>.fifo
BEAT = HERE / "heartbeat.txt"        # rewritten once a minute, so a supervisor can see the server is doing its job

# The machines live in devices.json next to this file (copy devices.example.json; it is not in git, since it names
# your machines). Without it the dashboard shows just the machine it runs on. Which device the server itself runs
# on is said on the command line (--here).
DEVICES_FILE = HERE / "devices.json"


def load_devices(path=DEVICES_FILE):
    """The device list: devices.json, or this machine alone when there is none."""
    if Path(path).exists():
        devices = json.loads(Path(path).read_text())
        ids = [d["id"] for d in devices]
        if len(set(ids)) != len(ids):
            raise ValueError(f"{path}: device ids must be unique")
        return devices
    kind = "mac" if sys.platform == "darwin" else "linux"
    return [{"id": "local", "label": socket.gethostname().split(".")[0], "kind": kind, "ssh": None}]


DEVICES = load_devices()

INTERVAL = 3          # seconds between samples
MAX_LIFE = 600        # an agent exits after this long; the server reconnects (no orphans if SSH drops)
STALE = 15            # no sample for this long = offline
HUNG = 30             # no sample for this long while the agent process is alive = kill and reconnect
LIVE_POINTS = 300     # 15 min of live history at INTERVAL
LONG_POINTS = 1440    # 24 h of one-minute aggregates

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "ServerAliveInterval=5",
       "-o", "ServerAliveCountMax=2", "-o", "LogLevel=ERROR"]
# The script arrives on stdin. A plain ReadToEnd() waits forever if the session dies before stdin closes, and the
# agent's own "is my SSH session gone" check never gets to run (agents piled up on a Windows PC). So read the
# raw stream with a 30 s deadline; Stream.ReadAsync runs on a pool thread in .NET Framework, so Wait() can time out.
WIN_CMD = ('powershell -NoProfile -ExecutionPolicy Bypass -Command "'
           '$s = [Console]::OpenStandardInput(); $m = New-Object IO.MemoryStream; $b = New-Object byte[] 65536; '
           '$end = (Get-Date).AddSeconds(30); '
           'while ($true) { $t = $s.ReadAsync($b, 0, $b.Length); '
           'if (-not $t.Wait([int][math]::Max(1, ($end - (Get-Date)).TotalMilliseconds))) { exit 3 }; '
           'if ($t.Result -le 0) { break }; $m.Write($b, 0, $t.Result) }; '
           '[Text.Encoding]::UTF8.GetString($m.ToArray()) | Invoke-Expression"')

# NVML clocks-throttle-reason bits (nvidia-smi clocks_throttle_reasons.active).
THROTTLE = {0x1: "idle", 0x2: "app clock setting", 0x4: "power cap", 0x8: "hw slowdown", 0x10: "sync boost",
            0x20: "sw thermal slowdown", 0x40: "hw thermal slowdown", 0x80: "power brake", 0x100: "display clock"}
THROTTLE_BAD = 0x8 | 0x20 | 0x40 | 0x80

# Alert thresholds.
GPU_TEMP = (80, 88)        # NVIDIA core temperature: warn, critical
PC_CPU_TEMP = (88, 95)     # desktop CPU (Ryzen Tctl); 95 is where a 9000-series part throttles
MAC_TEMP = (95, 103)       # Apple silicon die temperature (it runs hot by design)
VRAM_PCT = 95
MEM_PCT = 92
DISK_FREE_PCT = (10, 5)    # warn, critical


# ---------- pure functions (covered by test_fleet.py) ----------
def kind_from_uname(returncode, stdout):
    """The agent kind from running `uname -s` over SSH. Windows has no uname, so a clean failure means Windows;
    255 is SSH itself failing (unreachable, host key, login), which is not an answer."""
    if returncode == 255:
        return None
    return {"Linux": "linux", "Darwin": "mac"}.get(stdout.strip(), "win")


def detect_kind(alias):
    p = subprocess.run(SSH + [alias, "uname -s"], capture_output=True, text=True, timeout=20)
    kind = kind_from_uname(p.returncode, p.stdout)
    if not kind:
        raise OSError((p.stderr.strip().splitlines() or ["ssh failed"])[-1])
    return kind


def place(devices, here):
    """The device list as seen from the machine `here`: that one is read by a local process, a device pushed from
    another machine from the pipe its pusher writes to, every other one over SSH."""
    if here not in [d["id"] for d in devices]:
        raise ValueError(f"--here must be one of: {', '.join(d['id'] for d in devices)}")
    return [{**d, "via": "local" if d["id"] == here else "push" if d.get("push_from") not in (None, here) else "ssh"} for d in devices]


def aliases(dev):
    """The SSH aliases to try for this device, in order; [None] for a machine that is not read over SSH."""
    if dev.get("via", "ssh") != "ssh":
        return [None]
    return dev["ssh"] if isinstance(dev["ssh"], list) else [dev["ssh"]]


def agent_command(dev, kind=None, alias=None):
    """(argv, stdin text) that starts this device's agent, locally or over the given alias (default: its first)."""
    kind, alias = kind or dev["kind"], alias or aliases(dev)[0]
    if kind in ("mac", "linux"):
        script = (HERE / f"agent_{kind}.py").read_text()
        cmd = f"{'/usr/bin/python3' if kind == 'mac' else 'python3'} -u - {INTERVAL} {MAX_LIFE}"
        argv = cmd.split() if not alias else SSH + [alias, cmd]
        return argv, script
    script = f"$Interval = {INTERVAL}; $MaxLife = {MAX_LIFE}\n" + (HERE / "agent_win.ps1").read_text()
    return SSH + [alias, WIN_CMD], script


def decode_throttle(raw):
    try:
        bits = int(str(raw), 16)
    except (TypeError, ValueError):
        return [], False
    return [name for bit, name in THROTTLE.items() if bits & bit], bool(bits & THROTTLE_BAD)


# The schema of an agent sample. Agent output is untrusted input (a compromised machine could send anything), so
# clean() rebuilds every sample against these four sets and nothing else gets through:
TEXT_KEYS = {"kind", "host", "os", "model", "name", "uuid", "driver", "pstate", "throttle_raw", "pressure", "kinds",
             "server"}
DICT_KEYS = {"cpu", "mem", "power", "disk_io", "net"}                                             # top level: a dict
NESTED_DICTS = {"power": {"battery"}}                                                              # one level down
ROW_LISTS = {"gpus", "disks", "temps", "fans", "procs_cpu", "procs_mem", "gpu_procs", "models"}    # top level: dicts
VALUE_LISTS = {"cores", "load", "kinds"}                                                            # lists of scalars
BIG = 1e15            # no real reading is larger; beyond it arithmetic overflows into Infinity or OverflowError


def _scalar(v, key):
    """A text field keeps (capped) text; every other field is a finite, sane number, a bool or None."""
    if isinstance(v, str):
        return v[:200] if key in TEXT_KEYS else None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v if v == v and -BIG <= v <= BIG else None
    return None


def clean(sample, dicts=DICT_KEYS, rows=ROW_LISTS):
    """Rebuild an agent sample against the schema: text only in TEXT_KEYS, dicts and row lists only where the
    schema names them at that depth (a GPU row's "power" is a number, the top-level "power" is a dict), scalar
    lists only under VALUE_LISTS. Anything else becomes None, so the code downstream never meets a dict where it
    expects a number. A single item where a list belongs is re-wrapped: PowerShell unwraps one-element arrays."""
    if not isinstance(sample, dict):
        raise ValueError("an agent sample must be a JSON object")
    out = {}
    for k, v in list(sample.items())[:64]:
        k = str(k)[:40]
        if k in rows:
            items = v if isinstance(v, list) else [v]
            out[k] = None if v is None else [clean(r, (), ()) for r in items[:256] if isinstance(r, dict)]
        elif k in VALUE_LISTS:
            vals = v if isinstance(v, list) else [v]
            out[k] = None if v is None else [_scalar(x, k) for x in vals[:256]]
        elif k in dicts:
            out[k] = clean(v, NESTED_DICTS.get(k, ()), ()) if isinstance(v, dict) else None
        else:
            out[k] = _scalar(v, k)
    return out


def own_names():
    host = socket.gethostname().lower()
    short = host.split(".")[0]
    names = {"localhost", host, short, short + ".local"}
    try:        # the Bonjour name is set apart from the hostname on macOS (e.g. office-mac.lan vs Office-Mac-mini.local)
        local = subprocess.run(["/usr/sbin/scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                               timeout=5).stdout.strip().lower()
        if local:
            names |= {local, local + ".local"}
    except (OSError, subprocess.SubprocessError):
        pass
    return names


def host_allowed(header, names):
    """True when the Host header is an IP literal or one of this machine's names. A DNS-rebinding page reaches the
    server under the attacker's own domain name, so refusing every other name stops it reading /api/state."""
    host = (header or "").strip().lower()
    if host.startswith("["):                                  # [::1]:8766
        name = host[1:host.find("]")] if "]" in host else ""
    else:
        name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not name:
        return False
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return name in names


def _list(v):
    return v if isinstance(v, list) else [] if v is None else [v]


def normalise(sample):
    """Make an agent sample safe to use: rebuilt against the schema (clean), every list present, throttle decoded."""
    sample = clean(sample)
    for k in ("gpus", "disks", "temps", "fans", "procs_cpu", "procs_mem", "gpu_procs"):
        sample[k] = _list(sample.get(k))
    sample["models"] = _list(sample["models"]) if sample.get("models") is not None else None
    for k in ("cpu", "mem", "power", "disk_io", "net"):
        sample[k] = sample.get(k) or {}
    sample["cpu"]["cores"] = _list(sample["cpu"].get("cores"))
    sample["cpu"]["kinds"] = [k if k in ("E", "P") else "" for k in _list(sample["cpu"].get("kinds"))]
    by_uuid = {}
    for g in sample["gpus"]:
        g["throttle"], g["throttle_bad"] = decode_throttle(g.get("throttle_raw")) if "throttle_raw" in g else ([], False)
        by_uuid[g.get("uuid")] = g.get("index")
    for p in sample["gpu_procs"]:
        p["gpu"] = by_uuid.get(p.get("uuid"))
    return sample


def pct(used, total):
    if used is None or not total:
        return None
    v = used / total * 100
    return round(v, 1) if math.isfinite(v) else None


def watts(sample):
    """The power this device measures: whole-system watts on a Mac (SMC), the sum of GPU watts on a PC."""
    p = sample.get("power", {})
    return p.get("system_w") if p.get("system_w") is not None else p.get("gpu_w")


def compact(sample):
    """One history point: the few numbers the charts draw."""
    return {"t": round(sample["t"], 1), "cpu": sample["cpu"].get("util"),
            "mem": pct(sample["mem"].get("used"), sample["mem"].get("total")), "ct": sample["cpu"].get("temp"),
            "w": watts(sample),
            "g": [[g.get("util"), pct(g.get("mem_used"), g.get("mem_total")), g.get("temp"), g.get("power")]
                  for g in sample["gpus"]]}


def _agg(values, how):
    v = [x for x in values if x is not None]
    if not v:
        return None
    return round(max(v) if how == "max" else sum(v) / len(v), 1)


def aggregate(points, minute):
    """One-minute aggregate of live points: averages for load and power, maxima for memory, VRAM and temperature."""
    ngpu = max((len(p["g"]) for p in points), default=0)
    col = lambda i, j: [p["g"][i][j] for p in points if i < len(p["g"])]
    return {"t": minute, "n": len(points), "cpu": _agg([p["cpu"] for p in points], "avg"),
            "mem": _agg([p["mem"] for p in points], "max"), "ct": _agg([p["ct"] for p in points], "max"),
            "w": _agg([p["w"] for p in points], "avg"),
            "g": [[_agg(col(i, 0), "avg"), _agg(col(i, 1), "max"), _agg(col(i, 2), "max"), _agg(col(i, 3), "avg")]
                  for i in range(ngpu)]}


def alerts(states):
    """[{dev, level, msg}] from the current device states; level is 'crit' or 'warn'."""
    out = []

    def add(dev, level, msg):
        out.append({"dev": dev, "level": level, "msg": msg})

    for s in states:
        try:
            _device_alerts(s, add)
        except Exception as e:      # a sample the checks cannot read is itself worth an alert, never a dead stream
            add(s["label"], "warn", f"sample could not be checked ({type(e).__name__})")
    return sorted(out, key=lambda a: a["level"] != "crit")


def _device_alerts(s, add):
    name, x = s["label"], s.get("sample")
    if not s["online"]:
        add(name, "warn", "offline" + (f": {s['error']}" if s.get("error") else ""))
        return
    if not x:
        return
    mac = x.get("kind") == "mac"
    for g in x["gpus"]:
        gname = f"GPU {g.get('index', 0)} {g.get('name', '')}".strip()
        warn, crit = MAC_TEMP if mac else GPU_TEMP
        if g.get("temp") is not None and g["temp"] >= warn:
            add(name, "crit" if g["temp"] >= crit else "warn", f"{gname} at {g['temp']:.0f} °C")
        if g.get("throttle_bad"):
            bad = [r for r in g["throttle"] if r not in ("idle", "app clock setting", "power cap", "sync boost", "display clock")]
            add(name, "crit", f"{gname} is throttling: {', '.join(bad)}")
        v = pct(g.get("mem_used"), g.get("mem_total"))
        if not g.get("unified") and v is not None and v >= VRAM_PCT:
            add(name, "warn", f"{gname} VRAM {v:.0f}% full")
        if g.get("fan") == 0 and g.get("temp") is not None and g["temp"] >= 70:
            add(name, "warn", f"{gname} fan at 0% while at {g['temp']:.0f} °C")
    ct, (warn, crit) = x["cpu"].get("temp"), MAC_TEMP if mac else PC_CPU_TEMP
    if ct is not None and ct >= warn:
        add(name, "crit" if ct >= crit else "warn", f"CPU at {ct:.0f} °C")
    pressure = x["mem"].get("pressure")
    if pressure in ("warn", "critical"):
        add(name, "crit" if pressure == "critical" else "warn", f"memory pressure {pressure}")
    elif pressure is None and (pct(x["mem"].get("used"), x["mem"].get("total")) or 0) >= MEM_PCT:
        add(name, "warn", f"memory {pct(x['mem']['used'], x['mem']['total']):.0f}% used")
    for d in x["disks"]:
        free = pct(d.get("free"), d.get("total"))
        if free is not None and free < DISK_FREE_PCT[0]:
            add(name, "crit" if free < DISK_FREE_PCT[1] else "warn",
                f"disk {d.get('name', '')} {free:.0f}% free ({d['free'] / 2 ** 30:.0f} GB)")


def load_history(directory, now, devices):
    """The last 24 h of minute aggregates per device, from the JSONL files written by earlier runs."""
    out = {d: [] for d in devices}
    for path in sorted(Path(directory).glob("*.jsonl"))[-2:]:
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("d") in out and now - row.get("t", 0) < LONG_POINTS * 60:
                out[row.pop("d")].append(row)
    return {d: sorted(rows, key=lambda r: r["t"])[-LONG_POINTS:] for d, rows in out.items()}


# ---------- state ----------
class Fleet:
    def __init__(self, devices, history_dir):
        self.cond = threading.Condition()
        self.version = 0
        self.history_dir = Path(history_dir)
        self.history_dir.mkdir(parents=True, exist_ok=True)
        old = load_history(self.history_dir, time.time(), [d["id"] for d in devices])
        self.dev = {d["id"]: {"cfg": d, "sample": None, "seen": 0.0, "error": None, "connects": 0, "online": False,
                              "live": collections.deque(maxlen=LIVE_POINTS), "n_live": 0,
                              "long": collections.deque(old[d["id"]], maxlen=LONG_POINTS), "n_long": 0,
                              "bucket": [], "proc": None, "started": 0.0, "kind": d["kind"]} for d in devices}

    def _bump(self):
        self.version += 1
        self.cond.notify_all()

    def ingest(self, dev_id, sample):
        d = self.dev[dev_id]
        sample = normalise(sample)
        sample["lag"] = round(time.time() - sample["t"], 2)       # clock offset + transport delay
        sample["t"] = time.time()                                  # one clock for every chart: this machine's
        point = compact(sample)
        minute = int(point["t"] // 60) * 60
        with self.cond:
            if d["bucket"] and int(d["bucket"][0]["t"] // 60) * 60 != minute:
                self._close_minute(dev_id)
            d["bucket"].append(point)
            d["sample"], d["seen"], d["online"], d["error"] = sample, sample["t"], True, None
            d["live"].append(point)
            d["n_live"] += 1
            self._bump()

    def _close_minute(self, dev_id):
        d = self.dev[dev_id]
        agg = aggregate(d["bucket"], int(d["bucket"][0]["t"] // 60) * 60)
        d["bucket"] = []
        d["long"].append(agg)
        d["n_long"] += 1
        day = datetime.fromtimestamp(agg["t"]).strftime("%Y-%m-%d")
        with open(self.history_dir / f"{day}.jsonl", "a") as f:
            f.write(json.dumps({"d": dev_id, **agg}, separators=(",", ":")) + "\n")

    def disconnected(self, dev_id, error):
        with self.cond:
            self.dev[dev_id]["error"] = error
            self._bump()

    def watchdog(self):
        """Mark silent devices offline; kill an agent that is alive but has stopped reporting."""
        beat = 0
        while True:
            time.sleep(2)
            now = time.time()
            with self.cond:
                for d in self.dev.values():
                    if d["online"] and now - d["seen"] > STALE:
                        d["online"] = False
                        self._bump()
                    p = d["proc"]
                    if p and p.poll() is None and now - max(d["seen"], d["started"]) > HUNG:
                        p.kill()
                up = [i for i, d in self.dev.items() if d["online"]]
            if now - beat >= 60:
                beat = now
                try:
                    BEAT.write_text(f"{datetime.now():%Y-%m-%d %H:%M} {len(up)}/{len(self.dev)} online: {' '.join(up)}\n")
                except OSError:
                    pass

    def state(self, dev_id):
        d = self.dev[dev_id]
        return {"id": dev_id, "label": d["cfg"]["label"], "kind": d["kind"], "online": d["online"],
                "seen": d["seen"], "error": d["error"], "connects": d["connects"], "sample": d["sample"]}

    def snapshot(self):
        with self.cond:
            states = [self.state(i) for i in self.dev]
            return {"t": time.time(), "interval": INTERVAL, "devices": states, "alerts": alerts(states)}


def take(fleet, dev_id, line):
    """Ingest one agent line. -> (1, None) when it was a usable sample, else (0, why not)."""
    if not line.startswith(b"{"):
        return 0, None
    try:
        fleet.ingest(dev_id, json.loads(line))
        return 1, None
    except Exception as e:      # untrusted input: a sample we cannot use is reported, never fatal
        return 0, f"bad sample: {e!r}"[:200]


def run_pushed(fleet, dev, path):
    """Read a device that pushes. Opening the pipe waits until its pusher connects; the end of the stream is the
    pusher leaving (lid closed, off the network, or its agent's planned exit), and we wait for the next one."""
    d = fleet.dev[dev["id"]]
    if not (path.exists() and stat.S_ISFIFO(path.stat().st_mode)):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        os.mkfifo(path, 0o600)
    fleet.disconnected(dev["id"], "waiting for this machine to push")
    while True:
        error = None
        try:
            with open(path, "rb") as f:
                d["connects"] += 1
                for line in f:
                    error = take(fleet, dev["id"], line)[1] or error
        except OSError as e:
            error = str(e) or repr(e)
        fleet.disconnected(dev["id"], error or "waiting for this machine to push")
        time.sleep(1)


def run_device(fleet, dev):
    """Keep one agent stream alive for this device, reconnecting with backoff."""
    if dev["via"] == "push":
        return run_pushed(fleet, dev, PUSH / f"{dev['id']}.fifo")
    d, backoff, route = fleet.dev[dev["id"]], 2, 0
    while True:
        started, error, got, code = time.time(), None, 0, None
        alias = aliases(dev)[route % len(aliases(dev))]
        try:
            kind = detect_kind(alias) if dev["kind"] == "auto" else dev["kind"]
            d["kind"] = kind
            argv, script = agent_command(dev, kind, alias)
            p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            d["proc"], d["started"] = p, started
            d["connects"] += 1
            err_tail = collections.deque(maxlen=5)
            threading.Thread(target=_feed, args=(p, script), daemon=True).start()
            threading.Thread(target=lambda: err_tail.extend(l.decode("utf-8", "replace").strip() for l in p.stderr),
                             daemon=True).start()
            for line in p.stdout:
                ok, why = take(fleet, dev["id"], line)
                got, error = got + ok, why or error
            code = p.wait()
            time.sleep(0.1)
            error = error or next((l for l in reversed(err_tail) if l), None)
        except (OSError, subprocess.SubprocessError) as e:
            error = str(e) or repr(e)
        ran = time.time() - started
        if code == 0 and got and ran >= MAX_LIFE * 0.8:      # the agent's planned exit: reconnect at once
            backoff = 2
            time.sleep(0.5)
            continue
        if len(aliases(dev)) > 1:
            error = f"{alias}: {error or 'agent stopped'}"
        fleet.disconnected(dev["id"], error or "agent stopped")
        if not got:
            route += 1                       # this alias gave nothing: the next attempt uses the next one
        time.sleep(backoff if route % len(aliases(dev)) == 0 or got else 1)
        backoff = 2 if ran > 60 else min(30, backoff * 2)


def _feed(p, script):
    try:
        p.stdin.write(script.encode())
        p.stdin.close()
    except OSError:
        pass


# ---------- HTTP ----------
def make_handler(fleet, names):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not host_allowed(self.headers.get("Host"), names):
                return self.send_error(403, "Host not allowed (see --allow-host)")
            if self.path == "/api/state":
                return self._send(json.dumps(fleet.snapshot()).encode(), "application/json")
            if self.path.startswith("/fonts/") and self.path[7:] in FONTS:           # exact names only: no paths, no ..
                return self._send((HERE / "fonts" / self.path[7:]).read_bytes(), "font/woff2")
            if self.path != "/events":
                return self._send(PAGE.read_bytes(), "text/html; charset=utf-8")      # re-read: page edits show on refresh
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = None                                   # per device: (n_live, n_long) already sent
            seen = -1
            try:
                while True:
                    with fleet.cond:
                        fleet.cond.wait_for(lambda: fleet.version != seen, timeout=20)
                        seen = fleet.version
                        states = [fleet.state(i) for i in fleet.dev]
                        msg = {"t": time.time(), "interval": INTERVAL, "devices": states, "alerts": alerts(states),
                               "init": sent is None, "live": {}, "long": {}}
                        for i, d in fleet.dev.items():
                            nl, ng = (0, 0) if sent is None else sent[i]
                            if sent is None:
                                msg["live"][i], msg["long"][i] = list(d["live"]), list(d["long"])
                            else:
                                msg["live"][i] = list(d["live"])[len(d["live"]) - min(len(d["live"]), d["n_live"] - nl):]
                                msg["long"][i] = list(d["long"])[len(d["long"]) - min(len(d["long"]), d["n_long"] - ng):]
                        sent = {i: (d["n_live"], d["n_long"]) for i, d in fleet.dev.items()}
                    self.wfile.write(f"data: {json.dumps(msg, separators=(',', ':'))}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
    return H


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    local = [d["id"] for d in DEVICES if not d.get("ssh") and not d.get("push_from")]
    ap.add_argument("--here", default=local[0] if len(local) == 1 else None, metavar="ID",
                    help=f"which machine this server runs on: {', '.join(d['id'] for d in DEVICES)}")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to open it to the home network (no login on the page)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="extra DNS name the page may be opened under (IP addresses and this Mac's own names always work)")
    ap.add_argument("--history", default=str(HERE / "history"))
    a = ap.parse_args()
    if not a.here:
        ap.error("say which device this server runs on: --here ID")
    try:
        devices = place(DEVICES, a.here)
    except ValueError as e:
        ap.error(str(e))
    fleet = Fleet(devices, a.history)
    for dev in devices:
        threading.Thread(target=run_device, args=(fleet, dev), daemon=True).start()
    threading.Thread(target=fleet.watchdog, daemon=True).start()
    print(f"fleet dashboard on http://{a.host}:{a.port}", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(fleet, own_names() | {n.lower() for n in a.allow_host})).serve_forever()
