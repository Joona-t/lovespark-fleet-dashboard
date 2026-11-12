"""Fleet agent for macOS (Apple silicon): prints one JSON sample per tick on stdout.

    /usr/bin/python3 -u agent_mac.py [interval_s] [max_life_s]      (or piped: python3 -u - 3 600 < agent_mac.py)

Standard library only, no sudo, Python 3.9+. Everything is measured on this machine:
  CPU per core   host_processor_info tick deltas          Temperatures  IOHID thermal sensors
  Power          IOReport "Energy Model" deltas, SMC PSTR  Fans          SMC F<n>Ac
  GPU            ioreg IOAccelerator PerformanceStatistics Memory        vm_stat + sysctl
The agent exits when stdout closes or after max_life_s, so a dropped SSH session never leaves it running.
"""
import collections
import ctypes
import ctypes.util
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from ctypes import POINTER, addressof, byref, c_char_p, c_double, c_int, c_int32, c_int64, c_long, c_ubyte, c_uint32, \
    c_uint64, c_ushort, c_void_p

CF = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
IOK = ctypes.CDLL(ctypes.util.find_library("IOKit"))
LIBC = ctypes.CDLL(ctypes.util.find_library("c"))
UTF8 = 0x08000100

CF.CFStringCreateWithCString.restype = c_void_p
CF.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
CF.CFStringGetCString.restype = ctypes.c_bool
CF.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
CF.CFRelease.argtypes = [c_void_p]
CF.CFArrayGetCount.restype = c_long
CF.CFArrayGetCount.argtypes = [c_void_p]
CF.CFArrayGetValueAtIndex.restype = c_void_p
CF.CFArrayGetValueAtIndex.argtypes = [c_void_p, c_long]
CF.CFDictionaryGetValue.restype = c_void_p
CF.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]
CF.CFNumberCreate.restype = c_void_p
CF.CFNumberCreate.argtypes = [c_void_p, c_long, c_void_p]
CF.CFDictionaryCreate.restype = c_void_p
CF.CFDictionaryCreate.argtypes = [c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_long, c_void_p, c_void_p]


def cfstr(s):
    return CF.CFStringCreateWithCString(None, s.encode(), UTF8)


def pystr(ref):
    buf = ctypes.create_string_buffer(256)
    return buf.value.decode("utf-8", "replace") if ref and CF.CFStringGetCString(ref, buf, 256, UTF8) else ""


def sh(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""


def sysctl(*keys):
    return sh("/usr/sbin/sysctl", "-n", *keys).strip().splitlines()


# ---------- temperatures: IOHID thermal sensors (usage page 0xff00, usage 5) ----------
class Temps:
    def __init__(self):
        IOK.IOHIDEventSystemClientCreate.restype = c_void_p
        IOK.IOHIDEventSystemClientCreate.argtypes = [c_void_p]
        IOK.IOHIDEventSystemClientSetMatching.argtypes = [c_void_p, c_void_p]
        IOK.IOHIDEventSystemClientCopyServices.restype = c_void_p
        IOK.IOHIDEventSystemClientCopyServices.argtypes = [c_void_p]
        IOK.IOHIDServiceClientCopyProperty.restype = c_void_p
        IOK.IOHIDServiceClientCopyProperty.argtypes = [c_void_p, c_void_p]
        IOK.IOHIDServiceClientCopyEvent.restype = c_void_p
        IOK.IOHIDServiceClientCopyEvent.argtypes = [c_void_p, c_int64, c_int32, c_int64]
        IOK.IOHIDEventGetFloatValue.restype = c_double
        IOK.IOHIDEventGetFloatValue.argtypes = [c_void_p, c_int32]
        page, usage = c_int32(0xFF00), c_int32(5)
        keys = (c_void_p * 2)(cfstr("PrimaryUsagePage"), cfstr("PrimaryUsage"))
        vals = (c_void_p * 2)(CF.CFNumberCreate(None, 3, byref(page)), CF.CFNumberCreate(None, 3, byref(usage)))
        match = CF.CFDictionaryCreate(None, keys, vals, 2,
                                      addressof(c_void_p.in_dll(CF, "kCFTypeDictionaryKeyCallBacks")),
                                      addressof(c_void_p.in_dll(CF, "kCFTypeDictionaryValueCallBacks")))
        client = IOK.IOHIDEventSystemClientCreate(None)
        IOK.IOHIDEventSystemClientSetMatching(client, match)
        services = IOK.IOHIDEventSystemClientCopyServices(client)
        product = cfstr("Product")
        self.sensors = []
        for i in range(CF.CFArrayGetCount(services) if services else 0):
            s = CF.CFArrayGetValueAtIndex(services, i)
            self.sensors.append((pystr(IOK.IOHIDServiceClientCopyProperty(s, product)), s))

    def read(self):
        """{group: [values]} where group is the sensor name without its index, e.g. 'pACC MTR Temp Sensor'."""
        groups = collections.defaultdict(list)
        for name, s in self.sensors:
            ev = IOK.IOHIDServiceClientCopyEvent(s, 15, 0, 0)
            if not ev:
                continue
            v = IOK.IOHIDEventGetFloatValue(ev, 15 << 16)
            CF.CFRelease(ev)
            if 0 < v < 150:
                groups[re.sub(r"\s*\d+$", "", name) or "sensor"].append(v)
        return groups


# ---------- SMC: fans and total system power ----------
class SMC:
    class Vers(ctypes.Structure):
        _fields_ = [("major", c_ubyte), ("minor", c_ubyte), ("build", c_ubyte), ("reserved", c_ubyte), ("release", c_ushort)]

    class PLimit(ctypes.Structure):
        _fields_ = [("version", c_ushort), ("length", c_ushort), ("cpu", c_uint32), ("gpu", c_uint32), ("mem", c_uint32)]

    class KeyInfo(ctypes.Structure):
        _fields_ = [("dataSize", c_uint32), ("dataType", c_uint32), ("dataAttributes", c_ubyte)]

    class KeyData(ctypes.Structure):
        pass

    KeyData._fields_ = [("key", c_uint32), ("vers", Vers), ("pLimitData", PLimit), ("keyInfo", KeyInfo),
                        ("result", c_ubyte), ("status", c_ubyte), ("data8", c_ubyte), ("data32", c_uint32),
                        ("bytes", c_ubyte * 32)]

    def __init__(self):
        assert ctypes.sizeof(self.KeyData) == 80
        IOK.IOServiceMatching.restype = c_void_p
        IOK.IOServiceMatching.argtypes = [c_char_p]
        IOK.IOServiceGetMatchingService.restype = c_uint32
        IOK.IOServiceGetMatchingService.argtypes = [c_uint32, c_void_p]
        IOK.IOServiceOpen.argtypes = [c_uint32, c_uint32, c_uint32, POINTER(c_uint32)]
        IOK.IOConnectCallStructMethod.argtypes = [c_uint32, c_uint32, c_void_p, ctypes.c_size_t, c_void_p,
                                                  POINTER(ctypes.c_size_t)]
        service = IOK.IOServiceGetMatchingService(0, IOK.IOServiceMatching(b"AppleSMC"))
        self.conn = c_uint32()
        task = c_uint32.in_dll(LIBC, "mach_task_self_").value
        self.ok = bool(service) and IOK.IOServiceOpen(service, task, 0, byref(self.conn)) == 0
        self.info = {}

    def _call(self, inp):
        out, size = self.KeyData(), ctypes.c_size_t(80)
        if IOK.IOConnectCallStructMethod(self.conn, 2, byref(inp), 80, byref(out), byref(size)) or out.result:
            return None
        return out

    def read(self, key):
        """A numeric SMC key ('flt ', 'ui8 ', 'ui16', 'ui32'), or None when the key does not exist here."""
        if not self.ok:
            return None
        code = struct.unpack(">I", key.encode())[0]
        if key not in self.info:
            q = self.KeyData()
            q.key, q.data8 = code, 9                      # kSMCGetKeyInfo
            r = self._call(q)
            self.info[key] = (r.keyInfo.dataSize, struct.pack(">I", r.keyInfo.dataType)) if r else None
        if not self.info[key]:
            return None
        size, typ = self.info[key]
        q = self.KeyData()
        q.key, q.data8, q.keyInfo.dataSize = code, 5, size   # kSMCReadKey
        r = self._call(q)
        if not r:
            return None
        raw = bytes(r.bytes[:size])
        if typ == b"flt ":
            return struct.unpack("<f", raw)[0]
        if typ in (b"ui8 ", b"ui16", b"ui32"):
            return int.from_bytes(raw, "big")
        return None

    def temp_keys(self):
        """Every float temperature key for CPU cores (Tp*, Te*) and GPU (Tg*), found by walking the key table once."""
        keys = []
        for i in range(int(self.read("#KEY") or 0)):
            q = self.KeyData()
            q.data8, q.data32 = 8, i                      # kSMCGetKeyFromIndex
            r = self._call(q)
            if not r:
                continue
            k = struct.pack(">I", r.key).decode("latin-1")
            if k[:2] in ("Tp", "Te", "Tg") and self.read(k) is not None and self.info[k][1] == b"flt ":
                keys.append(k)
        return keys


# ---------- IOReport "Energy Model": CPU / GPU / ANE / DRAM watts from energy deltas ----------
class Energy:
    def __init__(self):
        L = self.L = ctypes.CDLL("/usr/lib/libIOReport.dylib")
        L.IOReportCopyChannelsInGroup.restype = c_void_p
        L.IOReportCopyChannelsInGroup.argtypes = [c_void_p, c_void_p, c_uint64, c_uint64, c_uint64]
        L.IOReportCreateSubscription.restype = c_void_p
        L.IOReportCreateSubscription.argtypes = [c_void_p, c_void_p, POINTER(c_void_p), c_uint64, c_void_p]
        L.IOReportCreateSamples.restype = c_void_p
        L.IOReportCreateSamples.argtypes = [c_void_p, c_void_p, c_void_p]
        L.IOReportCreateSamplesDelta.restype = c_void_p
        L.IOReportCreateSamplesDelta.argtypes = [c_void_p, c_void_p, c_void_p]
        L.IOReportChannelGetChannelName.restype = c_void_p
        L.IOReportChannelGetChannelName.argtypes = [c_void_p]
        L.IOReportChannelGetUnitLabel.restype = c_void_p
        L.IOReportChannelGetUnitLabel.argtypes = [c_void_p]
        L.IOReportSimpleGetIntegerValue.restype = c_int64
        L.IOReportSimpleGetIntegerValue.argtypes = [c_void_p, c_int32]
        channels = L.IOReportCopyChannelsInGroup(cfstr("Energy Model"), None, 0, 0, 0)
        self.subbed = c_void_p()
        self.sub = L.IOReportCreateSubscription(None, channels, byref(self.subbed), 0, None)
        self.key = cfstr("IOReportChannels")
        self.prev, self.prev_t = self._sample(), time.time()

    def _sample(self):
        return self.L.IOReportCreateSamples(self.sub, self.subbed, None) if self.sub else None

    def read(self):
        """Average watts since the previous call, only for the rails whose counters moved: {'gpu': W, ...}."""
        cur, now = self._sample(), time.time()
        out = {}
        if cur and self.prev and now > self.prev_t:
            delta = self.L.IOReportCreateSamplesDelta(self.prev, cur, None)
            arr = CF.CFDictionaryGetValue(delta, self.key) if delta else None
            for i in range(CF.CFArrayGetCount(arr) if arr else 0):
                ch = CF.CFArrayGetValueAtIndex(arr, i)
                name = pystr(self.L.IOReportChannelGetChannelName(ch))
                which = ("cpu" if name.endswith("CPU Energy") else "gpu" if name == "GPU Energy" else
                         "ane" if name.startswith("ANE") else "dram" if name.startswith("DRAM") else None)
                if not which:
                    continue
                unit = pystr(self.L.IOReportChannelGetUnitLabel(ch)).strip()
                scale = {"mJ": 1e-3, "uJ": 1e-6, "nJ": 1e-9}.get(unit)
                raw = self.L.IOReportSimpleGetIntegerValue(ch, 0)
                if scale and raw > 0:       # a counter that did not move is "not reported" here, never 0 W
                    out[which] = out.get(which, 0.0) + raw * scale / (now - self.prev_t)
            if delta:
                CF.CFRelease(delta)
        if self.prev:
            CF.CFRelease(self.prev)
        self.prev, self.prev_t = cur, now
        return out


# ---------- CPU per core ----------
LIBC.mach_host_self.restype = c_uint32
LIBC.host_processor_info.argtypes = [c_uint32, c_int, POINTER(c_uint32), POINTER(POINTER(c_int32)), POINTER(c_uint32)]
LIBC.vm_deallocate.argtypes = [c_uint32, c_void_p, ctypes.c_size_t]
HOST = LIBC.mach_host_self()


def cpu_ticks():
    """Per core (busy, total) tick counters."""
    n, info, cnt = c_uint32(), POINTER(c_int32)(), c_uint32()
    if LIBC.host_processor_info(HOST, 2, byref(n), byref(info), byref(cnt)):      # PROCESSOR_CPU_LOAD_INFO
        return []
    rows = []
    for i in range(n.value):
        user, system, idle, nice = (info[i * 4 + j] & 0xFFFFFFFF for j in range(4))
        rows.append((user + system + nice, user + system + nice + idle))
    LIBC.vm_deallocate(c_uint32.in_dll(LIBC, "mach_task_self_").value, ctypes.cast(info, c_void_p), cnt.value * 4)
    return rows


def cpu_util(prev, cur):
    out = []
    for (b0, t0), (b1, t1) in zip(prev, cur):
        dt = (t1 - t0) & 0xFFFFFFFF
        out.append(round(((b1 - b0) & 0xFFFFFFFF) / dt * 100, 1) if dt else 0.0)
    return out


# ---------- parsers (pure functions; covered by test_fleet.py) ----------
def parse_vm_stat(text, total):
    page = int(m.group(1)) if (m := re.search(r"page size of (\d+) bytes", text)) else 16384
    v = {k.strip(): int(n) for k, n in re.findall(r"^(.+?):\s+(\d+)\.", text, re.M)}
    # Activity Monitor's "Memory Used": app memory (anonymous - purgeable) + wired + compressed.
    used = (v.get("Anonymous pages", 0) - v.get("Pages purgeable", 0) + v.get("Pages wired down", 0)
            + v.get("Pages occupied by compressor", 0)) * page
    return {"total": total, "used": max(0, min(used, total)), "wired": v.get("Pages wired down", 0) * page,
            "compressed": v.get("Pages occupied by compressor", 0) * page,
            "cached": (v.get("File-backed pages", 0) + v.get("Pages purgeable", 0)) * page}


def parse_swap(text):
    def mb(k):
        m = re.search(k + r" = ([\d.]+)([MG])", text)
        return int(float(m.group(1)) * (1 << 30 if m.group(2) == "G" else 1 << 20)) if m else 0
    return mb("total"), mb("used")


def parse_gpu(text):
    def num(k):
        m = re.search(r'"%s"=(\d+)' % re.escape(k), text)
        return int(m.group(1)) if m else None
    m = re.search(r'"model" = "([^"]+)"', text)
    return {"name": m.group(1) if m else "Apple GPU", "util": num("Device Utilization %"),
            "renderer": num("Renderer Utilization %"), "tiler": num("Tiler Utilization %"),
            "mem_used": num("In use system memory"), "mem_alloc": num("Alloc system memory")}


def parse_netstat(text):
    """Total (rx, tx) bytes over the physical en* links."""
    rx = tx = 0
    for line in text.splitlines():
        c = line.split()
        if len(c) >= 10 and c[0].startswith("en") and c[2].startswith("<Link#"):
            rx, tx = rx + int(c[-5]), tx + int(c[-2])
    return rx, tx


def parse_disk_io(text):
    r = sum(int(x) for x in re.findall(r'"Bytes \(Read\)"=(\d+)', text))
    w = sum(int(x) for x in re.findall(r'"Bytes \(Write\)"=(\d+)', text))
    return r, w


def parse_ps(text, top=6):
    rows = []
    for line in text.splitlines():
        c = line.split(None, 3)
        if len(c) == 4:
            try:
                rows.append({"pid": int(c[0]), "cpu": float(c[1]), "mem": int(c[2]) * 1024, "name": c[3].strip()})
            except ValueError:
                pass
    return (sorted(rows, key=lambda r: -r["cpu"])[:top], sorted(rows, key=lambda r: -r["mem"])[:top])


def parse_battery(text):
    def num(k):
        m = re.search(r'"%s"\s*=\s*(-?\d+)' % k, text)
        return int(m.group(1)) if m else None
    if '"CurrentCapacity"' not in text:
        return None
    mw = lambda k: (num(k) / 1000.0) if num(k) is not None else None
    return {"pct": num("CurrentCapacity"), "charging": '"IsCharging" = Yes' in text,
            "plugged": '"ExternalConnected" = Yes' in text, "cycles": num("CycleCount"),
            "power_in_w": mw("SystemPowerIn"), "load_w": mw("SystemLoad")}


def summarise_temps(groups):
    """Sensor groups {name: [values]} -> (cpu_temp, gpu_temp, rows hottest first).

    Headline temperatures are the mean of each group (the SMC die sensors report several readings per core, so the
    mean is the stable figure); every row also carries its hottest reading. CPU prefers the SMC core sensors and
    falls back to the PMU die sensors on machines that expose no Tp*/Te* keys."""
    rows = sorted(({"name": k, "c": round(max(v), 1), "avg": round(sum(v) / len(v), 1), "n": len(v)}
                   for k, v in groups.items() if v), key=lambda r: -r["c"])
    by = {r["name"]: r for r in rows}
    cpu = by.get("CPU cores (SMC)") or by.get("PMU tdie")
    gpu = by.get("GPU (SMC)")
    return (cpu["avg"] if cpu else None, gpu["avg"] if gpu else None, rows)


def ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps", timeout=0.6) as r:
            return [{"name": m.get("name"), "vram": m.get("size_vram"), "size": m.get("size")}
                    for m in json.load(r).get("models", [])]
    except Exception:
        return None


def static_info():
    s = sysctl("hw.memsize", "machdep.cpu.brand_string", "hw.model", "hw.ncpu", "hw.perflevel0.logicalcpu",
               "hw.perflevel1.logicalcpu")
    s += [""] * (6 - len(s))
    n, p, e = int(s[3] or 0), int(s[4] or 0), int(s[5] or 0)
    boot = re.search(r"sec = (\d+)", " ".join(sysctl("kern.boottime")))
    return {"host": socket.gethostname().split(".")[0], "os": "macOS " + sh("/usr/bin/sw_vers", "-productVersion").strip(),
            "model": s[2], "cpu_model": s[1], "ncpu": n, "mem_total": int(s[0] or 0),
            # Apple silicon numbers the efficiency cluster first (cpu0..), then the performance cores.
            "core_kinds": (["E"] * e + ["P"] * p) if p + e == n else [],
            "boot": int(boot.group(1)) if boot else None}


def main():
    INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    MAX_LIFE = float(sys.argv[2]) if len(sys.argv) > 2 else 600.0
    info = static_info()
    temps, smc, energy = Temps(), SMC(), Energy()
    nfans = int(smc.read("FNum") or 0)
    smc_temps = smc.temp_keys()
    prev_cpu, prev_net, prev_io, prev_t, prev_io_t = cpu_ticks(), None, None, time.time(), time.time()
    started, tick, models = time.time(), 0, None
    # Slow-moving or costly readings refresh every third tick (RESEARCH.md: ~41 ms for the IOHID sensors, 20 ms for
    # ps, 12 ms for the disk ioreg); a machine with no battery is never asked again.
    hid, top_cpu, top_mem, io_rate = {}, [], [], [None, None]
    has_battery, battery = None, None
    time.sleep(min(1.0, INTERVAL))
    while time.time() - started < MAX_LIFE:
        t0 = time.time()
        cur_cpu = cpu_ticks()
        cores = cpu_util(prev_cpu, cur_cpu)
        prev_cpu = cur_cpu
        mem = parse_vm_stat(sh("/usr/bin/vm_stat"), info["mem_total"])
        extra = sysctl("vm.swapusage", "kern.memorystatus_vm_pressure_level")
        mem["swap_total"], mem["swap_used"] = parse_swap(extra[0] if extra else "")
        mem["pressure"] = {"1": "normal", "2": "warn", "4": "critical"}.get(extra[1].strip() if len(extra) > 1 else "")
        gpu = parse_gpu(sh("/usr/sbin/ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"))
        if tick % 3 == 0:
            hid = temps.read()
        groups = collections.defaultdict(list, {k: list(v) for k, v in hid.items()})
        for k in smc_temps:
            v = smc.read(k)
            if v and 0 < v < 150:
                groups["GPU (SMC)" if k[1] == "g" else "CPU cores (SMC)"].append(v)
        cpu_temp, gpu_temp, sensors = summarise_temps(groups)
        watts = energy.read()
        dt = max(1e-3, t0 - prev_t)
        net = parse_netstat(sh("/usr/sbin/netstat", "-ibn"))
        rate = lambda cur, prev, span: [max(0, round((c - p) / span)) for c, p in zip(cur, prev)] if prev else [None, None]
        net_rate = rate(net, prev_net, dt)
        prev_net, prev_t = net, t0
        if tick % 3 == 0:
            io = parse_disk_io(sh("/usr/sbin/ioreg", "-c", "IOBlockStorageDriver", "-r", "-w", "0", "-k", "Statistics"))
            io_rate = rate(io, prev_io, max(1e-3, t0 - prev_io_t))
            prev_io, prev_io_t = io, t0
            top_cpu, top_mem = parse_ps(sh("/bin/ps", "-Aceo", "pid=,pcpu=,rss=,comm="))
        if tick % 5 == 0:
            models = ollama_models()
        st = os.statvfs("/System/Volumes/Data")
        if has_battery is None or (has_battery and tick % 10 == 0):
            battery = parse_battery(sh("/usr/sbin/ioreg", "-r", "-n", "AppleSmartBattery", "-w", "0"))
            has_battery = battery is not None
        system_w = smc.read("PSTR")
        fans = [{"name": "fan %d" % i, "rpm": round(smc.read("F%dAc" % i) or 0),
                 "max": round(smc.read("F%dMx" % i) or 0)} for i in range(nfans)]
        sample = {
            "t": t0, "kind": "mac", "host": info["host"], "os": info["os"], "model": info["model"],
            "uptime": (t0 - info["boot"]) if info["boot"] else None,
            "cpu": {"model": info["cpu_model"], "util": round(sum(cores) / len(cores), 1) if cores else None,
                    "cores": cores, "kinds": info["core_kinds"], "load": [round(x, 2) for x in os.getloadavg()],
                    "temp": cpu_temp},
            "mem": mem,
            "gpus": [{"index": 0, "name": gpu["name"], "util": gpu["util"], "mem_used": gpu["mem_used"],
                      "mem_total": info["mem_total"], "unified": True, "temp": gpu_temp,
                      "power": round(watts["gpu"], 2) if "gpu" in watts else None,
                      "renderer": gpu["renderer"], "tiler": gpu["tiler"], "mem_alloc": gpu["mem_alloc"]}],
            "power": {"system_w": round(system_w, 2) if system_w else None,
                      "cpu_w": round(watts["cpu"], 2) if "cpu" in watts else None,
                      "gpu_w": round(watts["gpu"], 2) if "gpu" in watts else None,
                      "ane_w": round(watts["ane"], 2) if "ane" in watts else None,
                      "dram_w": round(watts["dram"], 2) if "dram" in watts else None, "battery": battery},
            "temps": sensors, "fans": fans,
            "disks": [{"name": "Macintosh HD", "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize}],
            "disk_io": {"r": io_rate[0], "w": io_rate[1]}, "net": {"rx": net_rate[0], "tx": net_rate[1]},
            "procs_cpu": top_cpu, "procs_mem": top_mem, "gpu_procs": [], "models": models,
            "agent_ms": None,
        }
        sample["agent_ms"] = round((time.time() - t0) * 1000)
        try:
            sys.stdout.write(json.dumps(sample, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            return
        tick += 1
        time.sleep(max(0.2, INTERVAL - (time.time() - t0)))


if __name__ == "__main__":
    main()
