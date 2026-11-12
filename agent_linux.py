"""Fleet agent for Linux: prints one JSON sample per tick on stdout.

    python3 -u agent_linux.py [interval_s] [max_life_s]      (or piped: python3 -u - 3 600 < agent_linux.py)

Standard library only, no root, Python 3.8+. Everything is measured on this machine:
  CPU per core   /proc/stat tick deltas                Temperatures, fans   /sys/class/hwmon (k10temp, coretemp, nvme, ...)
  GPUs           NVML via ctypes (nvidia-smi fallback)  CPU package watts    powercap RAPL, when the kernel lets a user read it
  Memory         /proc/meminfo                         Processes            /proc/<pid>/stat tick deltas
The agent exits when stdout closes or after max_life_s, so a dropped SSH session never leaves it running.
"""
import ctypes
import glob
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

GPU_FIELDS = ["index", "uuid", "name", "driver_version", "utilization.gpu", "utilization.memory", "memory.used",
              "memory.total", "temperature.gpu", "temperature.memory", "power.draw", "power.limit", "fan.speed",
              "clocks.current.graphics", "clocks.current.memory", "pcie.link.gen.current", "pcie.link.gen.max",
              "pcie.link.width.current", "pcie.link.width.max", "pstate", "clocks_throttle_reasons.active"]
CPU_CHIPS = ("k10temp", "zenpower", "coretemp", "cpu_thermal")
REAL_FS = ("ext4", "ext3", "xfs", "btrfs", "f2fs", "zfs", "ntfs", "ntfs3", "fuseblk", "exfat")
MIB = 1048576
ROOT = os.environ.get("FLEET_ROOT", "")      # tests point this at a recorded /proc and /sys tree; empty on a real machine


def read(path):
    try:
        with open(ROOT + path) as f:
            return f.read()
    except OSError:
        return ""


def ls(pattern):
    return sorted(p[len(ROOT):] for p in glob.glob(ROOT + pattern))


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout
    except Exception:
        return ""


def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------- parsers (pure functions; covered by test_fleet.py) ----------
def parse_stat(text):
    """/proc/stat -> ((busy, total) for the whole CPU, [(busy, total) per core]) in clock ticks."""
    rows = {}
    for line in text.splitlines():
        c = line.split()
        if c and c[0].startswith("cpu") and len(c) >= 9:
            v = [int(x) for x in c[1:9]]                 # user nice system idle iowait irq softirq steal
            rows[c[0]] = (sum(v) - v[3] - v[4], sum(v))
    cores = [rows[k] for k in sorted((k for k in rows if k != "cpu"), key=lambda k: int(k[3:]))]
    return rows.get("cpu", (0, 0)), cores


def util(prev, cur):
    dt = cur[1] - prev[1]
    return round(max(0.0, min(100.0, (cur[0] - prev[0]) / dt * 100)), 1) if dt > 0 else 0.0


def parse_meminfo(text):
    v = {k: int(n) * 1024 for k, n in re.findall(r"^(\w+):\s+(\d+) kB", text, re.M)}
    total = v.get("MemTotal", 0)
    return {"total": total, "used": max(0, total - v.get("MemAvailable", v.get("MemFree", 0))),
            "cached": v.get("Cached", 0) + v.get("SReclaimable", 0),
            "swap_total": v.get("SwapTotal", 0), "swap_used": v.get("SwapTotal", 0) - v.get("SwapFree", 0)}


def parse_cpuinfo(text):
    blocks = [b for b in text.split("\n\n") if "processor" in b]
    field = lambda b, k: (re.search(r"^%s\s*:\s*(.*)$" % re.escape(k), b, re.M) or [None, None])[1]
    mhz = [float(x) for x in (field(b, "cpu MHz") for b in blocks) if x]
    return {"model": (field(blocks[0], "model name") or "").strip() if blocks else "",
            "nthreads": len(blocks), "ncores": len({(field(b, "physical id"), field(b, "core id")) for b in blocks}),
            "freq_mhz": round(sum(mhz) / len(mhz)) if mhz else None}


def parse_nvidia(text):
    """nvidia-smi --query-gpu=<GPU_FIELDS> --format=csv,noheader,nounits -> the same GPU rows the Windows agent sends.
    Accepts the 20-field form too (drivers that no longer know clocks_throttle_reasons.active)."""
    gpus = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 20 or not f[0].isdigit():
            continue
        mem = lambda s: num(s) * MIB if num(s) is not None else None
        gpus.append({"index": int(f[0]), "uuid": f[1], "name": re.sub(r"^NVIDIA GeForce ", "", f[2]), "driver": f[3],
                     "util": num(f[4]), "mem_util": num(f[5]), "mem_used": mem(f[6]), "mem_total": mem(f[7]),
                     "temp": num(f[8]), "mem_temp": num(f[9]), "power": num(f[10]), "power_limit": num(f[11]),
                     "fan": num(f[12]), "clock": num(f[13]), "mem_clock": num(f[14]), "pcie_gen": num(f[15]),
                     "pcie_gen_max": num(f[16]), "pcie_width": num(f[17]), "pcie_width_max": num(f[18]),
                     "pstate": f[19], "throttle_raw": f[20] if len(f) > 20 else None})
    return gpus


class Nvml:
    """NVIDIA's management library through ctypes: one handle for the agent's life instead of launching nvidia-smi
    every tick (45 ms of CPU per tick on a dual-3090 box, against 2 ms here; RESEARCH.md). Same fields and units as
    parse_nvidia(). Raises OSError when the library or the driver is missing; the agent then falls back to nvidia-smi."""
    class Mem2(ctypes.Structure):           # nvmlMemory_v2_t: "used" excludes driver-reserved memory, like nvidia-smi
        _fields_ = [("version", ctypes.c_uint), ("total", ctypes.c_ulonglong), ("reserved", ctypes.c_ulonglong),
                    ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]

    class Util(ctypes.Structure):
        _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

    class Proc(ctypes.Structure):           # nvmlProcessInfo_v2_t
        _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong), ("gi", ctypes.c_uint), ("ci", ctypes.c_uint)]

    def __init__(self, lib="libnvidia-ml.so.1"):
        self.n = ctypes.CDLL(lib)
        if self.n.nvmlInit_v2() != 0:
            raise OSError("nvmlInit failed")
        buf = ctypes.create_string_buffer(96)
        self.driver = buf.value.decode() if self.n.nvmlSystemGetDriverVersion(buf, 96) == 0 else None
        count = ctypes.c_uint()
        self.n.nvmlDeviceGetCount_v2(ctypes.byref(count))
        self.handles = []
        for i in range(count.value):
            h = ctypes.c_void_p()
            if self.n.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h)) == 0:
                self.handles.append((i, h))

    def _uint(self, fn, h, *args):
        v = ctypes.c_uint()
        return v.value if getattr(self.n, fn)(h, *args, ctypes.byref(v)) == 0 else None

    def _text(self, fn, h):
        buf = ctypes.create_string_buffer(96)
        return buf.value.decode() if getattr(self.n, fn)(h, buf, 96) == 0 else None

    def gpus(self):
        out = []
        for i, h in self.handles:
            m = self.Mem2(); m.version = ctypes.sizeof(self.Mem2) | (2 << 24)
            mem_ok = self.n.nvmlDeviceGetMemoryInfo_v2(h, ctypes.byref(m)) == 0
            u = self.Util(); util_ok = self.n.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u)) == 0
            thr = ctypes.c_ulonglong()
            thr_ok = self.n.nvmlDeviceGetCurrentClocksThrottleReasons(h, ctypes.byref(thr)) == 0
            mw = lambda fn: (lambda v: round(v / 1000, 2) if v is not None else None)(self._uint(fn, h))
            pstate = self._uint("nvmlDeviceGetPerformanceState", h)
            out.append({"index": i, "uuid": self._text("nvmlDeviceGetUUID", h),
                        "name": re.sub(r"^NVIDIA (GeForce )?", "", self._text("nvmlDeviceGetName", h) or ""),
                        "driver": self.driver, "util": u.gpu if util_ok else None, "mem_util": u.memory if util_ok else None,
                        "mem_used": m.used if mem_ok else None, "mem_total": m.total if mem_ok else None,
                        "temp": self._uint("nvmlDeviceGetTemperature", h, 0), "mem_temp": None,
                        "power": mw("nvmlDeviceGetPowerUsage"), "power_limit": mw("nvmlDeviceGetEnforcedPowerLimit"),
                        "fan": self._uint("nvmlDeviceGetFanSpeed", h),
                        "clock": self._uint("nvmlDeviceGetClockInfo", h, 0), "mem_clock": self._uint("nvmlDeviceGetClockInfo", h, 2),
                        "pcie_gen": self._uint("nvmlDeviceGetCurrPcieLinkGeneration", h),
                        "pcie_gen_max": self._uint("nvmlDeviceGetMaxPcieLinkGeneration", h),
                        "pcie_width": self._uint("nvmlDeviceGetCurrPcieLinkWidth", h),
                        "pcie_width_max": self._uint("nvmlDeviceGetMaxPcieLinkWidth", h),
                        "pstate": "P%d" % pstate if pstate is not None and pstate < 32 else None,
                        "throttle_raw": "0x%016x" % thr.value if thr_ok else None})
        return out

    def procs(self, top=8):
        """Compute and graphics processes holding memory on each GPU, largest first (what nvidia-smi lists)."""
        rows = {}
        for i, h in self.handles:
            uuid = self._text("nvmlDeviceGetUUID", h)
            for fn in ("nvmlDeviceGetComputeRunningProcesses_v2", "nvmlDeviceGetGraphicsRunningProcesses_v2"):
                count, arr = ctypes.c_uint(64), (self.Proc * 64)()
                if getattr(self.n, fn)(h, ctypes.byref(count), arr) != 0:
                    continue
                for p in arr[:count.value]:
                    used = p.used if p.used < 2 ** 63 else None          # NVML_VALUE_NOT_AVAILABLE
                    argv0 = read("/proc/%d/cmdline" % p.pid).split("\0")[0]      # full name, no arguments
                    name = os.path.basename(argv0) or read("/proc/%d/comm" % p.pid).strip() or str(p.pid)
                    key = (p.pid, uuid)
                    if key not in rows or (used or 0) > (rows[key]["mem"] or 0):
                        rows[key] = {"pid": p.pid, "name": name, "uuid": uuid, "mem": used}
        return sorted(rows.values(), key=lambda r: -(r["mem"] or 0))[:top]


def parse_nvidia_apps(text, top=8):
    """nvidia-smi --query-compute-apps=pid,process_name,used_memory,gpu_uuid -> rows, largest VRAM first."""
    rows = []
    for line in text.splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) >= 4 and f[0].isdigit():
            rows.append({"pid": int(f[0]), "name": os.path.basename(",".join(f[1:-2])), "uuid": f[-1],
                         "mem": num(f[-2]) * MIB if num(f[-2]) is not None else None})
    return sorted(rows, key=lambda r: -(r["mem"] or 0))[:top]


def summarise_hwmon(readings):
    """[(chip, label, celsius)] -> (cpu_temp, rows hottest first). CPU is Tctl/Tdie/package when the chip names one."""
    cpu = [(label, c) for chip, label, c in readings if chip in CPU_CHIPS]
    named = [c for label, c in cpu if label in ("Tctl", "Tdie", "Package id 0")]
    groups = {}
    for chip, _, c in readings:
        groups.setdefault(chip, []).append(c)
    rows = sorted(({"name": k, "c": round(max(v), 1), "avg": round(sum(v) / len(v), 1), "n": len(v)}
                   for k, v in groups.items()), key=lambda r: -r["c"])
    return (round(named[0], 1) if named else round(max(c for _, c in cpu), 1) if cpu else None), rows


def parse_mounts(text):
    """/proc/mounts -> [mount point] of real filesystems, one per device, system mounts left out."""
    seen, out = set(), []
    for line in text.splitlines():
        c = line.split()
        if len(c) < 3 or c[2] not in REAL_FS or c[0] in seen:
            continue
        if c[1].startswith(("/snap", "/boot", "/var/lib/docker", "/run", "/sys", "/proc", "/dev")):
            continue
        seen.add(c[0])
        out.append(c[1].replace("\\040", " "))
    return out


def parse_diskstats(text):
    """/proc/diskstats -> total (read, written) bytes over whole disks (not partitions)."""
    r = w = 0
    for line in text.splitlines():
        c = line.split()
        if len(c) >= 10 and re.match(r"^(sd[a-z]+|vd[a-z]+|nvme\d+n\d+|mmcblk\d+)$", c[2]):
            r, w = r + int(c[5]) * 512, w + int(c[9]) * 512
    return r, w


def parse_netdev(text):
    """/proc/net/dev -> total (rx, tx) bytes over the physical links."""
    rx = tx = 0
    for line in text.splitlines():
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        c = rest.split()
        if len(c) >= 9 and not name.strip().startswith(("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "tailscale")):
            rx, tx = rx + int(c[0]), tx + int(c[8])
    return rx, tx


def parse_pid_stat(text):
    """/proc/<pid>/stat -> (pid, name, cpu ticks, resident pages), or None. The name may contain spaces and brackets."""
    a, b = text.find("("), text.rfind(")")
    f = text[b + 2:].split()
    if a < 0 or b < 0 or len(f) < 22:
        return None
    try:
        return int(text[:a]), text[a + 1:b], int(f[11]) + int(f[12]), int(f[21])
    except ValueError:
        return None


# ---------- readers ----------
def read_hwmon(root="/sys/class/hwmon"):
    temps, fans = [], []
    for d in ls(root + "/hwmon*"):
        chip = read(d + "/name").strip() or os.path.basename(d)
        for f in ls(d + "/temp*_input"):
            c = num(read(f))
            if c is not None and 0 < c / 1000 < 150:
                temps.append((chip, read(f[:-6] + "_label").strip(), c / 1000))
        for f in ls(d + "/fan*_input"):
            rpm = num(read(f))
            if rpm is not None:
                fans.append({"name": "%s %s" % (chip, read(f[:-6] + "_label").strip() or os.path.basename(f)[:-6]), "rpm": rpm})
    return temps, fans


def read_procs():
    out = {}
    for path in ls("/proc/[0-9]*/stat"):
        row = parse_pid_stat(read(path))
        if row:
            out[row[0]] = row[1:]
    return out


def models():
    """Models held in memory by a local Ollama (port 11434) or served by vLLM (port 8000). None when neither answers."""
    found, any_server = [], False
    for server, url, rows in (("Ollama", "http://127.0.0.1:11434/api/ps", lambda d: d.get("models", [])),
                              ("vLLM", "http://127.0.0.1:8000/v1/models", lambda d: d.get("data", []))):
        try:
            with urllib.request.urlopen(url, timeout=0.6) as r:
                data = json.load(r)
            any_server = True
            found += [{"name": m.get("name") or m.get("id"), "vram": m.get("size_vram"), "server": server} for m in rows(data)]
        except Exception:
            pass
    return found if any_server else None


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    max_life = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    cpu = parse_cpuinfo(read("/proc/cpuinfo"))
    pretty = re.search(r'^PRETTY_NAME="?([^"\n]*)', read("/etc/os-release"), re.M)
    os_name = "%s · Linux %s" % (pretty.group(1) if pretty else "Linux", os.uname().release)
    board = ("%s %s" % (read("/sys/class/dmi/id/board_vendor").strip(), read("/sys/class/dmi/id/board_name").strip())).strip()
    tck, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")
    rapl = "/sys/class/powercap/intel-rapl:0/energy_uj"
    fields = list(GPU_FIELDS)
    try:
        nvml = None if ROOT else Nvml()          # tests (FLEET_ROOT) exercise the nvidia-smi path
    except (OSError, AttributeError):
        nvml = None

    prev_total, prev_cores = parse_stat(read("/proc/stat"))
    prev_io, prev_net, prev_procs = parse_diskstats(read("/proc/diskstats")), parse_netdev(read("/proc/net/dev")), read_procs()
    prev_uj, prev_t, prev_scan = num(read(rapl)), time.time(), time.time()
    started, tick, loaded, gpu_procs, rows = time.time(), 0, None, [], []
    time.sleep(min(1.0, interval))
    while time.time() - started < max_life:
        t0 = time.time()
        dt = max(1e-3, t0 - prev_t)
        total, cores = parse_stat(read("/proc/stat"))
        io, net, uj = parse_diskstats(read("/proc/diskstats")), parse_netdev(read("/proc/net/dev")), num(read(rapl))
        if tick % 3 == 0:     # every process's /proc/<pid>/stat is most of a tick's reads; the table is fine at 9 s
            procs, scan_dt = read_procs(), max(1e-3, t0 - prev_scan)
            rows = [{"pid": pid, "name": name, "cpu": round((ticks - prev_procs[pid][1]) / tck / scan_dt * 100, 1), "mem": rss * page}
                    for pid, (name, ticks, rss) in procs.items() if pid in prev_procs]
            prev_procs, prev_scan = procs, t0
        gpus = nvml.gpus() if nvml else parse_nvidia(sh("nvidia-smi", "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"))
        if not gpus and not nvml and len(fields) == len(GPU_FIELDS):          # a driver that dropped the throttle field rejects the whole query
            fields = GPU_FIELDS[:-1]
            gpus = parse_nvidia(sh("nvidia-smi", "--query-gpu=" + ",".join(fields), "--format=csv,noheader,nounits"))
            if not gpus:
                fields = list(GPU_FIELDS)
        if tick % 3 == 0:
            gpu_procs = nvml.procs() if nvml else parse_nvidia_apps(sh(
                "nvidia-smi", "--query-compute-apps=pid,process_name,used_memory,gpu_uuid", "--format=csv,noheader,nounits"))
        if tick % 5 == 0:
            loaded = models()
        readings, fans = read_hwmon()
        cpu_temp, sensors = summarise_hwmon(readings)
        disks = []
        for m in parse_mounts(read("/proc/mounts")):
            try:
                st = os.statvfs(ROOT + m)
                disks.append({"name": m, "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize})
            except OSError:
                pass
        cpu_w = round((uj - prev_uj) / 1e6 / dt, 2) if uj is not None and prev_uj is not None and uj >= prev_uj else None
        gpu_w = round(sum(g["power"] for g in gpus if g["power"] is not None), 2) if gpus else None
        sample = {
            "t": t0, "kind": "linux", "host": socket.gethostname().split(".")[0], "os": os_name, "model": board,
            "uptime": num(read("/proc/uptime").split()[0]) if read("/proc/uptime") else None,
            "cpu": {"model": cpu["model"], "util": util(prev_total, total),
                    "cores": [util(p, c) for p, c in zip(prev_cores, cores)], "kinds": [],
                    "load": [round(x, 2) for x in os.getloadavg()], "temp": cpu_temp, "freq_mhz": parse_cpuinfo(read("/proc/cpuinfo"))["freq_mhz"],
                    "ncores": cpu["ncores"], "nthreads": cpu["nthreads"]},
            "mem": parse_meminfo(read("/proc/meminfo")), "gpus": gpus, "power": {"gpu_w": gpu_w, "cpu_w": cpu_w},
            "temps": sensors, "fans": fans, "disks": disks,
            "disk_io": {"r": round((io[0] - prev_io[0]) / dt), "w": round((io[1] - prev_io[1]) / dt)},
            "net": {"rx": round((net[0] - prev_net[0]) / dt), "tx": round((net[1] - prev_net[1]) / dt)},
            "procs_cpu": sorted(rows, key=lambda r: -r["cpu"])[:6], "procs_mem": sorted(rows, key=lambda r: -r["mem"])[:6],
            "gpu_procs": gpu_procs, "models": loaded, "agent_ms": None,
        }
        prev_total, prev_cores, prev_io, prev_net, prev_uj, prev_t = total, cores, io, net, uj, t0
        sample["agent_ms"] = round((time.time() - t0) * 1000)
        try:
            sys.stdout.write(json.dumps(sample, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            return
        tick += 1
        time.sleep(max(0.2, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
