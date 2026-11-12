"""Tests for the fleet dashboard's pure functions.    python3 -m unittest test_fleet -v

Fixtures are real output captured from a MacBook Pro (M1 Pro) and a Linux box (RTX 3090 Ti + 3090); GPU ids replaced.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

import agent_linux as lin
import agent_mac as mac
import fleet_dashboard as fd

EXAMPLE = fd.load_devices(Path(__file__).resolve().parent / "devices.example.json")

HERE = Path(__file__).resolve().parent
GB = 2 ** 30

VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                          190000.
Pages speculative:                         5000.
Pages wired down:                        100000.
Pages purgeable:                          20000.
Anonymous pages:                         400000.
File-backed pages:                       150000.
Pages occupied by compressor:            200000.
"""
IOACCEL = ('"PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=6343245824,'
           '"Tiler Utilization %"=43,"Renderer Utilization %"=42,"Device Utilization %"=43,"In use system memory"=670203904}\n'
           '      "model" = "Apple M1 Pro"')
NETSTAT = """Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                        500     0      90000      500     0      90000     0
en0        1500  <Link#11>   aa:bb:cc:dd:ee:ff   1000     0    5000000     800     0    2000000     0
en0        1500  10.0.0        10.0.0.20       1000     -    5000000     800     -    2000000     -
en5        1500  <Link#7>    aa:bb:cc:dd:ee:00     10     0       1000      10     0        500     0
utun3      1380  <Link#20>                         9     0        900       9     0        900     0
"""
BATTERY = ('  |   "ExternalConnected" = Yes\n  |   "PowerTelemetryData" = {"SystemLoad"=20128,"SystemPowerIn"=52440}\n'
           '  |   "CurrentCapacity" = 82\n  |   "CycleCount" = 2095\n  |   "IsCharging" = Yes\n')


def gpu(**kw):
    g = {"index": 0, "uuid": "GPU-a", "name": "RTX 3090 Ti", "util": 0, "mem_used": 596 * 2 ** 20,
         "mem_total": 24564 * 2 ** 20, "temp": 32, "power": 10.5, "power_limit": 480, "fan": 30,
         "throttle_raw": "0x0000000000000001"}
    g.update(kw)
    return g


def sample(**kw):
    s = {"t": 1000.0, "kind": "win", "cpu": {"util": 10, "cores": [10, 10], "temp": None},
         "mem": {"used": 8 * GB, "total": 64 * GB}, "gpus": [gpu()], "power": {"gpu_w": 10.5},
         "disks": [{"name": "C:", "total": 1000 * GB, "free": 500 * GB}], "gpu_procs": [], "models": None}
    s.update(kw)
    return fd.normalise(s)


def state(s, online=True, label="GPU box", error=None):
    return {"label": label, "online": online, "error": error, "sample": s}


class MacParsers(unittest.TestCase):
    def test_memory_used_is_app_plus_wired_plus_compressed(self):
        m = mac.parse_vm_stat(VM_STAT, 16 * GB)
        self.assertEqual(m["used"], (400000 - 20000 + 100000 + 200000) * 16384)
        self.assertEqual(m["compressed"], 200000 * 16384)

    def test_memory_used_never_exceeds_total(self):
        self.assertEqual(mac.parse_vm_stat(VM_STAT, 1 * GB)["used"], 1 * GB)

    def test_swap(self):
        self.assertEqual(mac.parse_swap("total = 3072.00M  used = 2473.94M  free = 598.06M  (encrypted)"),
                         (3072 * 2 ** 20, int(2473.94 * 2 ** 20)))
        self.assertEqual(mac.parse_swap(""), (0, 0))

    def test_gpu_takes_the_exact_key_not_the_driver_variant(self):
        g = mac.parse_gpu(IOACCEL)
        self.assertEqual((g["name"], g["util"], g["mem_used"], g["mem_alloc"]), ("Apple M1 Pro", 43, 670203904, 6343245824))

    def test_netstat_counts_each_physical_link_once(self):
        self.assertEqual(mac.parse_netstat(NETSTAT), (5001000, 2000500))

    def test_processes_sorted_both_ways(self):
        cpu, mem = mac.parse_ps("  10  5.0  1000 small\n  20 90.0   500 Google Chrome Helper (Renderer)\n  30  1.0 9000 big\n")
        self.assertEqual(cpu[0]["name"], "Google Chrome Helper (Renderer)")
        self.assertEqual((mem[0]["name"], mem[0]["mem"]), ("big", 9000 * 1024))

    def test_battery_and_desktop(self):
        b = mac.parse_battery(BATTERY)
        self.assertEqual((b["pct"], b["charging"], b["plugged"], b["cycles"], b["power_in_w"]), (82, True, True, 2095, 52.44))
        self.assertIsNone(mac.parse_battery(""))            # a Mac mini has no battery

    def test_temps_prefer_smc_core_sensors_and_fall_back_to_pmu(self):
        cpu, gpu_t, rows = mac.summarise_temps({"CPU cores (SMC)": [57.0, 67.0, 74.0], "GPU (SMC)": [60.0, 64.0], "PMU tdie": [51.0]})
        self.assertEqual((cpu, gpu_t, rows[0]["name"], rows[0]["c"]), (66.0, 62.0, "CPU cores (SMC)", 74.0))
        self.assertEqual(mac.summarise_temps({"PMU tdie": [50.0, 52.0]})[:2], (51.0, None))
        self.assertEqual(mac.summarise_temps({})[:2], (None, None))

    def test_cpu_util_survives_counter_wraparound(self):
        self.assertEqual(mac.cpu_util([(100, 200), (0xFFFFFFF0, 0xFFFFFFF0)], [(150, 300), (0x10, 0x30)]), [50.0, 50.0])
        self.assertEqual(mac.cpu_util([(5, 9)], [(5, 9)]), [0.0])


# Real nvidia-smi output from a dual-GPU Linux box (3090 Ti + 3090), captured 2026-11-11 with the agent's 21 fields.
BOX_SMI = """0, GPU-00000000-0000-0000-0000-000000000001, NVIDIA GeForce RTX 3090 Ti, 610.88, 1, 4, 596, 24564, 32, N/A, 10.51, 480.00, 30, 210, 405, 1, 4, 8, 16, P8, 0x0000000000000001
1, GPU-00000000-0000-0000-0000-000000000002, NVIDIA GeForce RTX 3090, 610.88, 0, 0, 0, 24576, 37, N/A, 7.58, 370.00, 0, 210, 405, 1, 4, 8, 16, P8, 0x0000000000000001
"""
PROC_STAT = """cpu  100 0 50 800 50 0 0 0 0 0
cpu0 60 0 20 400 20 0 0 0 0 0
cpu1 40 0 30 400 30 0 0 0 0 0
cpu10 1 0 1 1 1 0 0 0 0 0
intr 12345
"""
MEMINFO = """MemTotal:       65000000 kB
MemFree:         1000000 kB
MemAvailable:   50000000 kB
Cached:         10000000 kB
SReclaimable:    1000000 kB
SwapTotal:       8000000 kB
SwapFree:        6000000 kB
"""
CPUINFO = """processor\t: 0
model name\t: AMD Ryzen 9 9950X 16-Core Processor
cpu MHz\t\t: 3000.000
physical id\t: 0
core id\t\t: 0

processor\t: 1
model name\t: AMD Ryzen 9 9950X 16-Core Processor
cpu MHz\t\t: 5000.000
physical id\t: 0
core id\t\t: 0

processor\t: 2
model name\t: AMD Ryzen 9 9950X 16-Core Processor
cpu MHz\t\t: 4000.000
physical id\t: 0
core id\t\t: 1
"""
MOUNTS = """/dev/nvme0n1p2 / ext4 rw,relatime 0 0
/dev/nvme0n1p1 /boot/efi vfat rw 0 0
/dev/nvme0n1p2 /var/snap/x ext4 rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sda1 /mnt/big\\040disk ext4 rw 0 0
/dev/loop3 /snap/core/1 squashfs ro 0 0
"""
DISKSTATS = """ 259       0 nvme0n1 1000 0 2000 0 500 0 4000 0 0 0 0
 259       1 nvme0n1p1 10 0 20 0 5 0 40 0 0 0 0
   8       0 sda 100 0 200 0 50 0 400 0 0 0 0
   7       0 loop0 9 0 9 0 0 0 0 0 0 0 0
"""
NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 999 9 0 0 0 0 0 0 999 9 0 0 0 0 0 0
enp5s0: 5000 50 0 0 0 0 0 0 2000 20 0 0 0 0 0 0
docker0: 77 1 0 0 0 0 0 0 77 1 0 0 0 0 0 0
wlan0: 100 1 0 0 0 0 0 0 300 3 0 0 0 0 0 0
"""


class LinuxParsers(unittest.TestCase):
    def test_nvidia_rows_match_what_the_windows_agent_sends(self):
        g = lin.parse_nvidia(BOX_SMI)
        self.assertEqual([(x["index"], x["name"], x["temp"], x["power"], x["power_limit"], x["fan"]) for x in g],
                         [(0, "RTX 3090 Ti", 32.0, 10.51, 480.0, 30.0), (1, "RTX 3090", 37.0, 7.58, 370.0, 0.0)])
        self.assertEqual((g[0]["mem_used"], g[0]["mem_total"], g[0]["mem_temp"], g[0]["pcie_width"], g[0]["throttle_raw"]),
                         (596 * 2 ** 20, 24564 * 2 ** 20, None, 8.0, "0x0000000000000001"))
        s = fd.normalise({"t": 1.0, "kind": "linux", "gpus": g})        # and they survive the schema untouched
        self.assertEqual((s["gpus"][1]["name"], s["gpus"][1]["power"], s["gpus"][0]["throttle"]), ("RTX 3090", 7.58, ["idle"]))

    def test_nvidia_without_the_throttle_field_and_with_junk_lines(self):
        short = ", ".join(BOX_SMI.splitlines()[0].split(", ")[:20])
        g = lin.parse_nvidia("Failed to initialize NVML\n" + short + "\n\n")
        self.assertEqual((len(g), g[0]["pstate"], g[0]["throttle_raw"]), (1, "P8", None))
        self.assertEqual(lin.parse_nvidia(""), [])

    def test_gpu_processes_have_real_vram_on_linux(self):
        apps = lin.parse_nvidia_apps("4321, /usr/bin/python3, 20480, GPU-a\n99, /opt/x, y/vllm, [N/A], GPU-b\n")
        self.assertEqual([(a["pid"], a["name"], a["mem"], a["uuid"]) for a in apps],
                         [(4321, "python3", 20480 * 2 ** 20, "GPU-a"), (99, "vllm", None, "GPU-b")])

    def test_cpu_ticks_and_utilisation(self):
        total, cores = lin.parse_stat(PROC_STAT)
        self.assertEqual((total, cores), ((150, 1000), [(80, 500), (70, 500), (2, 4)]))      # iowait counts as idle; cpu10 sorts last
        self.assertEqual((lin.util((0, 0), (150, 1000)), lin.util((5, 5), (5, 5)), lin.util((0, 0), (9, 3))), (15.0, 0.0, 100.0))

    def test_memory_uses_available_not_free(self):
        m = lin.parse_meminfo(MEMINFO)
        self.assertEqual((m["total"], m["used"], m["swap_used"], m["cached"]),
                         (65000000 * 1024, 15000000 * 1024, 2000000 * 1024, 11000000 * 1024))

    def test_cpuinfo_counts_cores_not_threads(self):
        self.assertEqual(lin.parse_cpuinfo(CPUINFO), {"model": "AMD Ryzen 9 9950X 16-Core Processor", "nthreads": 3, "ncores": 2, "freq_mhz": 4000})
        self.assertEqual(lin.parse_cpuinfo("")["nthreads"], 0)

    def test_cpu_temperature_prefers_tctl(self):
        cpu, rows = lin.summarise_hwmon([("k10temp", "Tccd1", 70.0), ("k10temp", "Tctl", 62.5), ("nvme", "Composite", 45.0), ("nvme", "", 49.0)])
        self.assertEqual((cpu, rows[0], rows[1]["c"]), (62.5, {"name": "k10temp", "c": 70.0, "avg": 66.2, "n": 2}, 49.0))
        self.assertEqual(lin.summarise_hwmon([("coretemp", "Core 0", 55.0), ("coretemp", "Core 1", 58.0)])[0], 58.0)
        self.assertEqual(lin.summarise_hwmon([("nvme", "Composite", 45.0)])[0], None)

    def test_mounts_disks_and_network(self):
        self.assertEqual(lin.parse_mounts(MOUNTS), ["/", "/mnt/big disk"])
        self.assertEqual(lin.parse_diskstats(DISKSTATS), (2200 * 512, 4400 * 512))               # whole disks only
        self.assertEqual(lin.parse_netdev(NETDEV), (5100, 2300))                                 # no loopback, no docker bridge

    def test_process_names_with_spaces_and_brackets(self):
        rest = " S 1 1 1 0 -1 4194304 100 0 0 0 250 50 0 0 20 0 1 0 100 1000000 2048 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0"
        self.assertEqual(lin.parse_pid_stat("4242 (tmux: server (1))" + rest), (4242, "tmux: server (1)", 300, 2048))
        self.assertIsNone(lin.parse_pid_stat("garbage"))


class LinuxAgentEndToEnd(unittest.TestCase):
    def test_agent_runs_over_a_recorded_proc_tree_and_feeds_the_server(self):
        """The whole agent loop, on this machine: a /proc and /sys tree on disk, a stand-in nvidia-smi that prints
        a real dual-GPU box's output, then the sample goes through the server's schema, history point and alerts."""
        pid_rest = " S 1 1 1 0 -1 4194304 100 0 0 0 250 50 0 0 20 0 1 0 100 1000000 2048 18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 3 0 0 0 0 0"
        files = {"proc/stat": PROC_STAT, "proc/meminfo": MEMINFO, "proc/cpuinfo": CPUINFO, "proc/uptime": "9000.5 1.0\n",
                 "proc/mounts": "/dev/nvme0n1p2 / ext4 rw 0 0\n", "proc/diskstats": DISKSTATS, "proc/net/dev": NETDEV,
                 "proc/4242/stat": "4242 (vllm serve)" + pid_rest, "etc/os-release": 'PRETTY_NAME="Ubuntu 26.04 LTS"\n',
                 "sys/class/hwmon/hwmon0/name": "k10temp\n", "sys/class/hwmon/hwmon0/temp1_input": "61500\n",
                 "sys/class/hwmon/hwmon0/temp1_label": "Tctl\n", "sys/class/hwmon/hwmon1/name": "nct6799\n",
                 "sys/class/hwmon/hwmon1/fan2_input": "1180\n", "sys/class/dmi/id/board_vendor": "ASUS\n",
                 "sys/class/dmi/id/board_name": "X870E\n", "sys/class/powercap/intel-rapl:0/energy_uj": "5000000\n"}
        with tempfile.TemporaryDirectory() as d:
            for rel, text in files.items():
                path = Path(d) / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            fake = Path(d) / "bin" / "nvidia-smi"
            fake.parent.mkdir()
            fake.write_text("#!/bin/sh\ncase \"$1\" in --query-gpu*) cat <<'X'\n" + BOX_SMI + "X\n;; *) echo '4242, /usr/bin/python3, 20480, GPU-00000000-0000-0000-0000-000000000002';; esac\n")
            fake.chmod(0o755)
            env = dict(os.environ, FLEET_ROOT=d, PATH=str(fake.parent) + os.pathsep + os.environ.get("PATH", ""))
            out = subprocess.run([sys.executable, str(HERE / "agent_linux.py"), "0.3", "1.4"], env=env, capture_output=True, text=True, timeout=30)
        lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
        self.assertGreaterEqual(len(lines), 2, out.stderr[-400:])            # it ticks, and it stops by itself at max life
        raw = json.loads(lines[-1])
        s = fd.normalise(json.loads(lines[-1]))
        self.assertEqual((s["kind"], s["os"].split(" · ")[0], s["model"], s["uptime"]), ("linux", "Ubuntu 26.04 LTS", "ASUS X870E", 9000.5))
        self.assertEqual((s["cpu"]["model"], s["cpu"]["temp"], s["cpu"]["ncores"], len(s["cpu"]["cores"]), s["cpu"]["util"]),
                         ("AMD Ryzen 9 9950X 16-Core Processor", 61.5, 2, 3, 0.0))
        self.assertEqual([(g["name"], g["temp"], g["power"]) for g in s["gpus"]], [("RTX 3090 Ti", 32.0, 10.51), ("RTX 3090", 37.0, 7.58)])
        self.assertEqual((s["power"]["gpu_w"], s["power"]["cpu_w"], s["fans"], s["disks"][0]["name"]), (18.09, 0.0, [{"name": "nct6799 fan2", "rpm": 1180.0}], "/"))
        self.assertEqual((s["gpu_procs"][0]["gpu"], s["gpu_procs"][0]["mem"], s["procs_mem"][0]["name"]), (1, 20480 * 2 ** 20, "vllm serve"))
        self.assertEqual(s, fd.clean(raw) | {k: s[k] for k in ("gpus", "gpu_procs")}, "the schema must not drop a field the Linux agent sends")
        host_disk = lambda a: a["msg"].startswith("disk /")       # statvfs reads the disk this test runs on, not the recording
        self.assertEqual((fd.compact({**s, "t": 1.0})["g"][1][:3], [a for a in fd.alerts([state(s)]) if not host_disk(a)]), ([0.0, 0.0, 37.0], []))


class Server(unittest.TestCase):
    def test_os_detection_from_uname(self):
        self.assertEqual([fd.kind_from_uname(0, "Linux\n"), fd.kind_from_uname(0, "Darwin\n"), fd.kind_from_uname(1, ""), fd.kind_from_uname(255, "")],
                         ["linux", "mac", "win", None])                                         # 255 = SSH itself failed: no answer

    def test_linux_agent_command_and_pc_cpu_alert(self):
        box = next(d for d in EXAMPLE if d["id"] == "gpu-box")
        self.assertEqual(fd.aliases(box), ["gpu-box-linux", "gpu-box-windows"])                 # one alias per OS: each has its own host key
        argv, script = fd.agent_command(box, "linux")
        self.assertEqual((argv[-2], argv[-1], "parse_nvidia" in script), ("gpu-box-linux", "python3 -u - 3 600", True))
        self.assertEqual(fd.agent_command(box, "win", "gpu-box-windows")[0][-2], "gpu-box-windows")
        self.assertEqual((fd.aliases({"ssh": None}), fd.aliases({"ssh": "my-pc"})), ([None], ["my-pc"]))
        hot = lambda kind, t: [a["level"] for a in fd.alerts([state(sample(kind=kind, cpu={"util": 1, "temp": t}))])]
        self.assertEqual((hot("linux", 87), hot("linux", 90), hot("linux", 96), hot("mac", 90)), ([], ["warn"], ["crit"], []))

    def test_throttle_idle_is_not_bad_thermal_is(self):
        self.assertEqual(fd.decode_throttle("0x0000000000000001"), (["idle"], False))
        self.assertEqual(fd.decode_throttle("0x0000000000000044"), (["power cap", "hw thermal slowdown"], True))
        self.assertEqual(fd.decode_throttle("[N/A]"), ([], False))

    def test_normalise_rewraps_what_powershell_unwrapped(self):
        s = fd.normalise({"t": 1.0, "cpu": {"cores": 7}, "mem": {}, "gpus": gpu(), "disks": {"name": "C:"},
                          "gpu_procs": {"pid": 4, "uuid": "GPU-a"}, "models": {"name": "qwen"}})
        self.assertEqual((len(s["gpus"]), s["disks"], s["cpu"]["cores"], s["models"]), (1, [{"name": "C:"}], [7], [{"name": "qwen"}]))
        self.assertEqual(s["gpu_procs"][0]["gpu"], 0)                # pid joined to its GPU by uuid
        self.assertEqual(s["procs_cpu"], [])
        self.assertIsNone(fd.normalise({"t": 1.0})["models"])        # None = Ollama not running, [] = running and empty

    def test_hostile_agent_output_cannot_reach_the_page_as_markup(self):
        evil = '"><img src=x onerror=alert(1)>'
        s = fd.normalise({"t": evil, "agent_ms": evil, "host": evil, "kind": "win", "uptime": float("nan"),
                          "cpu": {"util": evil, "cores": [evil, 5, float("inf")], "kinds": [evil, "E"], "temp": evil},
                          "mem": {"used": evil, "total": 1, "pressure": "warn"},
                          "gpus": [{"index": evil, "name": evil, "temp": evil, "fan": evil, evil: 1}, evil],
                          "disks": [{"name": "C:", "free": evil}], "temps": [{"name": "x", "c": evil, "n": evil}],
                          "procs_cpu": [{"pid": evil, "name": "p", "cpu": evil}], "gpu_procs": evil, "models": [{"name": "m", "vram": evil}]})

        def strings(v, key=None):
            if isinstance(v, dict):
                return [x for k, val in v.items() for x in strings(val, k)]
            if isinstance(v, list):
                return [x for val in v for x in strings(val, key)]
            return [(key, v)] if isinstance(v, str) else []
        left = strings(s)
        self.assertTrue(all(k in fd.TEXT_KEYS or k == "throttle" for k, _ in left), left)   # text only in named text fields
        self.assertEqual((s["agent_ms"], s["uptime"], s["cpu"]["cores"], s["cpu"]["kinds"]), (None, None, [None, 5, None], ["", "E"]))
        self.assertEqual((s["gpus"][0]["index"], len(s["gpus"]), s["procs_cpu"][0]["pid"], s["gpu_procs"]), (None, 1, None, []))
        json.loads(json.dumps(fd.compact({**s, "t": 1.0}), allow_nan=False))                # nothing JSON.parse would reject
        self.assertEqual(len(fd.clean({"name": "x" * 999})["name"]), 200)
        self.assertEqual(fd.clean({"gpus": [{"power": 9.5, "cpu": {"a": 1}}], "power": {"battery": {"pct": 80}, "gpu_w": {}}}),
                         {"gpus": [{"power": 9.5, "cpu": None}], "power": {"battery": {"pct": 80}, "gpu_w": None}})

    def test_no_shape_of_agent_output_can_break_the_stream(self):
        """Put every hostile value at every position of a full sample, one at a time. The server may refuse the sample
        (ingest raises one of the errors run_device catches) but whatever it accepts must survive alerts and JSON."""
        full = {"t": 1.0, "kind": "win", "host": "h", "os": "o", "model": "m", "uptime": 5.0, "agent_ms": 9,
                "cpu": {"model": "c", "util": 10, "cores": [1, 2], "kinds": ["E", "P"], "load": [1.0, 2.0, 3.0], "temp": 50, "freq_mhz": 3000},
                "mem": {"total": 64 * GB, "used": 8 * GB, "pressure": "normal", "swap_used": 0, "swap_total": 1},
                "gpus": [gpu(), gpu(index=1, uuid="GPU-b", unified=True)], "power": {"gpu_w": 10.5, "system_w": 20, "battery": {"pct": 80}},
                "temps": [{"name": "s", "c": 50, "avg": 49, "n": 2}], "fans": [{"name": "f", "rpm": 1000, "max": 5000}],
                "disks": [{"name": "C:", "total": 1000 * GB, "free": 500 * GB}], "disk_io": {"r": 1, "w": 2}, "net": {"rx": 1, "tx": 2},
                "procs_cpu": [{"pid": 1, "name": "p", "cpu": 5, "mem": 100}], "procs_mem": [{"pid": 1, "name": "p", "cpu": 5, "mem": 100}],
                "gpu_procs": [{"pid": 1, "name": "p", "uuid": "GPU-a", "mem": 100}], "models": [{"name": "q", "vram": 1, "size": 2}]}
        hostile = [{}, [], [[]], {"a": {"b": [1]}}, [{"temp": {}}], "text", "", True, None, -1, 0, 10 ** 400, 1e308, -1e308,
                   float("inf"), float("nan"), 1e-320]

        def variants(node):
            """Every copy of node with exactly one position replaced by one hostile value."""
            for h in hostile:
                yield h
            if isinstance(node, dict):
                for k in node:
                    for v in variants(node[k]):
                        yield {**node, k: v}
            elif isinstance(node, list):
                for i in range(len(node)):
                    for v in variants(node[i]):
                        yield node[:i] + [v] + node[i + 1:]

        tried = accepted = 0
        with tempfile.TemporaryDirectory() as d:
            for bad in variants(full):
                f = fd.Fleet([{"id": "box", "label": "GPU box", "kind": "win", "ssh": "box"}], d)
                tried += 1
                try:
                    f.ingest("box", json.loads(json.dumps(bad)) if isinstance(bad, dict) else bad)
                except (ValueError, KeyError, TypeError):          # exactly what run_device catches and reports
                    continue
                accepted += 1
                snap = f.snapshot()                                 # runs alerts() over the stored sample
                json.dumps(snap, allow_nan=False)                   # nothing JSON.parse in the page would reject
                json.dumps(list(f.dev["box"]["live"]), allow_nan=False)
        self.assertGreater(tried, 1500)
        self.assertGreater(accepted, tried // 2)                   # hostile fields are dropped, the sample still counts

    def test_host_header_allowlist_blocks_dns_rebinding(self):
        names = {"localhost", "macbookpro", "macbookpro.local"}
        for ok in ("127.0.0.1:8766", "localhost:8766", "LOCALHOST", "[::1]:8766", "10.0.0.20:8766", "100.64.0.9", "macbookpro.local:8766"):
            self.assertTrue(fd.host_allowed(ok, names), ok)
        for bad in ("evil.example:8766", "127.0.0.1.evil.example", "localhost.evil.example:8766", "", None, "[::1", "a:b:c"):
            self.assertFalse(fd.host_allowed(bad, names), bad)
        self.assertIn("localhost", fd.own_names())

    def test_compact_and_power_source(self):
        p = fd.compact(sample())
        self.assertEqual((p["cpu"], p["mem"], p["w"], p["g"]), (10, 12.5, 10.5, [[0, 2.4, 32, 10.5]]))
        self.assertEqual(fd.watts({"power": {"system_w": 22.1, "gpu_w": 0.8}}), 22.1)      # a Mac: whole machine
        self.assertIsNone(fd.watts({"power": {}}))

    def test_aggregate_averages_load_and_keeps_peaks(self):
        pts = [{"t": 60, "cpu": 10, "mem": 50, "ct": None, "w": 100, "g": [[0, 10, 40, 100]]},
               {"t": 63, "cpu": 30, "mem": 70, "ct": None, "w": 300, "g": [[100, 90, 80, 300]]}]
        a = fd.aggregate(pts, 60)
        self.assertEqual((a["n"], a["cpu"], a["mem"], a["ct"], a["w"], a["g"]), (2, 20.0, 70, None, 200.0, [[50.0, 90, 80, 200.0]]))

    def test_healthy_idle_gpu_box_has_no_alerts(self):
        self.assertEqual(fd.alerts([state(sample())]), [])

    def test_alerts(self):
        msgs = lambda s: [(a["level"], a["msg"]) for a in fd.alerts([state(s)])]
        self.assertEqual(msgs(sample(gpus=[gpu(temp=84)])), [("warn", "GPU 0 RTX 3090 Ti at 84 °C")])
        self.assertEqual(msgs(sample(gpus=[gpu(temp=91)]))[0][0], "crit")
        self.assertEqual(msgs(sample(gpus=[gpu(throttle_raw="0x40")])), [("crit", "GPU 0 RTX 3090 Ti is throttling: hw thermal slowdown")])
        self.assertEqual(msgs(sample(gpus=[gpu(mem_used=24000 * 2 ** 20)])), [("warn", "GPU 0 RTX 3090 Ti VRAM 98% full")])
        self.assertEqual(msgs(sample(gpus=[gpu(temp=72, fan=0)])), [("warn", "GPU 0 RTX 3090 Ti fan at 0% while at 72 °C")])
        self.assertEqual(msgs(sample(disks=[{"name": "C:", "total": 2000 * GB, "free": 180 * GB}])), [("warn", "disk C: 9% free (180 GB)")])
        self.assertEqual(msgs(sample(mem={"used": 60 * GB, "total": 64 * GB})), [("warn", "memory 94% used")])

    def test_mac_alerts_use_pressure_and_apple_temperatures(self):
        s = sample(kind="mac", mem={"used": 15 * GB, "total": 16 * GB, "pressure": "normal"},
                   gpus=[{"index": 0, "name": "Apple M1 Pro", "temp": 85, "unified": True, "mem_used": 15 * GB, "mem_total": 16 * GB}])
        self.assertEqual(fd.alerts([state(s)]), [])                  # 94% used, pressure normal, 85 degrees: fine on a Mac
        s["mem"]["pressure"] = "critical"
        self.assertEqual(fd.alerts([state(s)])[0]["level"], "crit")

    def test_offline_device_is_one_alert_and_critical_sorts_first(self):
        out = fd.alerts([state(None, online=False, label="Mac mini", error="timed out"), state(sample(gpus=[gpu(temp=95)]))])
        self.assertEqual([(a["dev"], a["level"]) for a in out], [("GPU box", "crit"), ("Mac mini", "warn")])
        self.assertEqual(out[1]["msg"], "offline: timed out")

    def test_history_round_trip_drops_old_and_unknown(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as d:
            rows = [{"d": "box", "t": now - 120, "cpu": 1}, {"d": "box", "t": now - 60, "cpu": 2},
                    {"d": "box", "t": now - 2 * 86400, "cpu": 9}, {"d": "gone", "t": now - 60, "cpu": 3}]
            (Path(d) / "2026-11-11.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
            h = fd.load_history(d, now, ["box", "mac"])
        self.assertEqual(([r["cpu"] for r in h["box"]], h["mac"]), ([1, 2], []))

    def test_fleet_ingest_closes_minutes_into_the_history_file(self):
        with tempfile.TemporaryDirectory() as d:
            f = fd.Fleet([{"id": "box", "label": "GPU box", "kind": "win", "ssh": "box"}], d)
            f.ingest("box", sample())
            f.dev["box"]["bucket"][0]["t"] -= 120                    # pretend the first point is two minutes old
            f.ingest("box", sample())
            self.assertEqual((len(f.dev["box"]["long"]), f.dev["box"]["n_live"], f.state("box")["online"]), (1, 2, True))
            self.assertEqual(json.loads(next(Path(d).glob("*.jsonl")).read_text())["d"], "box")

    def test_agent_commands(self):
        argv, script = fd.agent_command({"kind": "mac", "ssh": None})
        self.assertEqual((argv[0], "IOReport" in script), ("/usr/bin/python3", True))
        argv, script = fd.agent_command({"kind": "win", "ssh": "my-pc"})
        self.assertEqual([d["id"] for d in EXAMPLE], ["mini", "laptop", "pc", "gpu-box"])
        self.assertEqual((argv[0], argv[-2]), ("ssh", "my-pc"))
        self.assertTrue(script.startswith("$Interval = 3; $MaxLife = 600\n"))

    def test_the_server_reads_its_own_machine_locally_a_laptop_by_push_the_rest_over_ssh(self):
        via = lambda here: {d["id"]: d["via"] for d in fd.place(EXAMPLE, here)}
        self.assertEqual(via("mini"), {"mini": "local", "laptop": "push", "pc": "ssh", "gpu-box": "ssh"})
        self.assertEqual(via("laptop"), {"mini": "ssh", "laptop": "local", "pc": "ssh", "gpu-box": "ssh"})   # a dev run on the laptop
        self.assertRaises(ValueError, fd.place, EXAMPLE, "toaster")
        on_mini = {d["id"]: d for d in fd.place(EXAMPLE, "mini")}
        self.assertEqual(fd.agent_command(on_mini["mini"])[0][0], "/usr/bin/python3")             # no SSH to itself
        self.assertEqual(fd.aliases(on_mini["laptop"]), [None])
        from_laptop = {d["id"]: d for d in fd.place(EXAMPLE, "laptop")}
        self.assertEqual(fd.agent_command(from_laptop["mini"])[0][-2], "my-mini")

    def test_devices_come_from_a_file_or_default_to_this_machine(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual([x["id"] for x in fd.load_devices(Path(d) / "missing.json")], ["local"])
            dup = Path(d) / "dup.json"
            dup.write_text('[{"id": "a", "kind": "mac", "ssh": null}, {"id": "a", "kind": "win", "ssh": "x"}]')
            self.assertRaises(ValueError, fd.load_devices, dup)

    def test_a_pushed_device_comes_online_from_its_pipe_and_survives_rubbish(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(fd, "PUSH", Path(d) / "push"):
            dev = {"id": "mac", "label": "MacBook Pro", "kind": "mac", "ssh": None, "push_from": "mac", "via": "push"}
            f, pipe = fd.Fleet([dev], d), Path(d) / "push" / "mac.fifo"
            threading.Thread(target=fd.run_device, args=(f, dev), daemon=True).start()
            deadline = time.time() + 5
            while not pipe.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(f.state("mac")["error"], "waiting for this machine to push")
            with open(pipe, "wb") as w:                                # the pusher connects
                w.write(b"not json\n{\"broken\n[1,2]\n" + json.dumps(sample()).encode() + b"\n")
            while not (f.dev["mac"]["n_live"] and f.dev["mac"]["error"]) and time.time() < deadline:   # ...and leaves
                time.sleep(0.02)
            st = f.state("mac")
            self.assertEqual((st["sample"]["kind"], f.dev["mac"]["n_live"], st["connects"]), ("win", 1, 1))
            self.assertIn("bad sample", st["error"])                   # the broken line is reported, the good one kept
            with open(pipe, "wb") as w:                                # the next pusher is picked up without a restart
                w.write(json.dumps(sample()).encode() + b"\n")
            while f.dev["mac"]["n_live"] < 2 and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(f.dev["mac"]["n_live"], 2)

    def test_windows_agent_start_gives_up_on_a_silent_session(self):
        """BUG-005: `[Console]::In.ReadToEnd()` waited forever on a session that went silent (client asleep or gone
        without closing), so the agent never loaded its own exit check; six such agents sat on one Windows PC. The start
        command must read stdin with a deadline and exit when it passes."""
        cmd = fd.WIN_CMD
        self.assertNotIn("ReadToEnd()", cmd)
        for part in ("OpenStandardInput", "ReadAsync", ".Wait(", "exit 3", "Invoke-Expression"):
            self.assertIn(part, cmd)
        self.assertEqual(fd.agent_command({"kind": "win", "ssh": "pc"})[0][-1], cmd)

    def test_windows_agent_is_ascii(self):                           # it travels through the console code page
        (HERE / "agent_win.ps1").read_bytes().decode("ascii")


if __name__ == "__main__":
    unittest.main()
