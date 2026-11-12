#!/usr/bin/env python3
"""What does watching a Linux machine cost that machine? Three designs, measured on the target itself.

    python3 bench/bench_linux.py <ssh-host> [rounds]        rounds default: 5

  stream  ours: one SSH session, agent_linux.py piped in, one JSON line every 3 s
  poll    agentless: a new SSH connection every 3 s that reads the same sources (procfs, hwmon, nvidia-smi x2)
  mux     agentless, tuned: the same probe every 3 s over one reused connection (OpenSSH ControlMaster)

Cost on the target = CPU of everything the design runs there: the commands (bash `times`, children user+sys) plus
the sshd processes serving them (/proc/<pid>/schedstat, ns). Rounds alternate stream, poll, mux so drift and
warmup hit all three. Each round covers TICKS ticks at 3 s. Also recorded: client-side wall time per tick (how
fresh a reading is), bytes on the wire (ssh -v "Transferred"), and errors (non-zero exits, empty output).
"""
import json
import os
import re
import statistics as st
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
HOST = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: bench_linux.py <ssh-host> [rounds]")
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
TICKS, INTERVAL = 10, 3
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]
GPU_Q = ("index,name,uuid,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,"
         "power.limit,fan.speed,clocks.sm,clocks.mem,pstate,pcie.link.gen.current,pcie.link.width.current,"
         "pcie.link.gen.max,pcie.link.width.max,driver_version,temperature.memory,clocks_throttle_reasons.active")
# The same sources the agent reads in one tick (CPU, memory, disks, network, every process, sensors, GPUs).
PROBE = ("cat /proc/stat /proc/meminfo /proc/diskstats /proc/net/dev /proc/mounts /proc/loadavg >/dev/null; "
         "cat /proc/[0-9]*/stat >/dev/null 2>&1; cat /sys/class/hwmon/hwmon*/temp*_input /sys/class/hwmon/hwmon*/fan*_input >/dev/null 2>&1; "
         f"nvidia-smi --query-gpu={GPU_Q} --format=csv,noheader,nounits; "
         "nvidia-smi --query-compute-apps=pid,process_name,used_memory,gpu_uuid --format=csv,noheader,nounits")
# After the work: the commands' CPU (bash times, 2nd line = children) and both sshd processes of this session.
TAIL = ('echo "@@TIMES"; times; set -- $(cat /proc/$PPID/stat); '        # $4 = the parent sshd ([priv]) pid
        'echo "@@SSHD $(cat /proc/$PPID/schedstat) | $(cat /proc/$4/schedstat)"')


def secs(t):                       # "0m1.230s" -> 1.23
    m, s = re.match(r"(\d+)m([\d.]+)s", t).groups()
    return int(m) * 60 + float(s)


def parse(out):
    """-> (children cpu s, sshd cpu s) from the TAIL block."""
    lines = out.splitlines()
    i = lines.index("@@TIMES")
    child = sum(secs(x) for x in lines[i + 2].split())
    sshd = sum(int(part.split()[0]) for part in lines[i + 3].split("@@SSHD ", 1)[1].split("|")) / 1e9
    return child, sshd


def sent(err):
    m = re.search(r"Transferred: sent (\d+), received (\d+) bytes", err)
    return int(m[1]) + int(m[2]) if m else 0


def stream():
    script = (HERE / "agent_linux.py").read_text()
    cmd = f"bash -c 'python3 -u - {INTERVAL} {TICKS * INTERVAL - 1}; {TAIL}'"
    t0 = time.time()
    p = subprocess.run(["ssh", "-v", "-o", "BatchMode=yes", HOST, cmd], input=script, capture_output=True, text=True)
    wall = time.time() - t0
    samples = [l for l in p.stdout.splitlines() if l.startswith("{")]
    ok = p.returncode == 0 and len(samples) >= TICKS - 1 and all(json.loads(s).get("gpus") for s in samples)
    child, sshd = parse(p.stdout)
    lat = [json.loads(s)["agent_ms"] / 1000 for s in samples if "agent_ms" in json.loads(s)]
    return dict(ticks=len(samples), cpu=child + sshd, child=child, sshd=sshd, bytes=sent(p.stderr),
                fresh=st.median(lat) if lat else None, errors=0 if ok else 1, wall=wall)


def polls(mux):
    ctl = Path(tempfile.mkdtemp()) / "m"
    extra = ["-o", "ControlMaster=auto", "-o", f"ControlPath={ctl}", "-o", "ControlPersist=60"] if mux else []
    cpu = child = sshd = byt = errors = 0
    lat = []
    if mux:   # open the shared connection first (it logs its own traffic), then read its sshd pids' baseline
        log = ctl.with_suffix(".log")
        master = subprocess.Popen(["ssh", "-v", "-N", "-o", "BatchMode=yes", "-o", "ControlMaster=yes",
                                   "-o", f"ControlPath={ctl}", HOST], stderr=open(log, "w"))
        while not ctl.exists():
            time.sleep(0.05)
        base = parse(subprocess.run(SSH + extra + [HOST, f"bash -c '{TAIL}'"], capture_output=True, text=True).stdout)
    for _ in range(TICKS):
        t0 = time.time()
        p = subprocess.run(["ssh", "-v"] + SSH[1:] + extra + [HOST, f"bash -c '{PROBE} >/dev/null; {TAIL}'"],
                           capture_output=True, text=True)
        lat.append(time.time() - t0)
        if p.returncode != 0 or "@@TIMES" not in p.stdout:
            errors += 1
        else:
            c, s = parse(p.stdout)
            child += c
            sshd = s if mux else sshd + s           # mux: the shared sshd's counter keeps growing; take the last
            byt += sent(p.stderr)
        time.sleep(max(0, INTERVAL - (time.time() - t0)))
    if mux:
        sshd -= base[1]
        subprocess.run(SSH + extra + ["-O", "exit", HOST], capture_output=True)
        master.wait(10)
        byt = sent(log.read_text())          # the whole shared connection, handshake included, over all ticks
    return dict(ticks=TICKS - errors, cpu=child + sshd, child=child, sshd=sshd, bytes=byt,
                fresh=st.median(lat), errors=errors)


def main():
    runs = {"stream": [], "poll": [], "mux": []}
    for r in range(ROUNDS):
        for name, fn in (("stream", stream), ("poll", lambda: polls(False)), ("mux", lambda: polls(True))):
            res = fn()
            runs[name].append(res)
            print(f"round {r + 1} {name:6} cpu/tick {res['cpu'] / max(1, res['ticks']) * 1000:6.1f} ms "
                  f"(cmds {res['child'] / max(1, res['ticks']) * 1000:5.1f}, sshd {res['sshd'] / max(1, res['ticks']) * 1000:5.1f}) "
                  f"bytes/tick {res['bytes'] / max(1, res['ticks']):7.0f}  fresh {res['fresh']:.3f} s  errors {res['errors']}", flush=True)
    print("\nmedian [range] over", ROUNDS, "rounds, per 3 s tick, CPU measured on", HOST)
    for name, rs in runs.items():
        per = lambda k: [x[k] / max(1, x["ticks"]) for x in rs]
        cpu = [v * 1000 for v in per("cpu")]
        b = per("bytes")
        fr = [x["fresh"] for x in rs]
        print(f"  {name:6} CPU {st.median(cpu):6.1f} ms [{min(cpu):.1f}-{max(cpu):.1f}]  "
              f"= {st.median(cpu) / 30:.2f}% of one core   bytes {st.median(b):7.0f}   "
              f"freshness {st.median(fr):.3f} s   errors {sum(x['errors'] for x in rs)}")
    Path(HERE / "bench" / f"results-{HOST}.json").write_text(json.dumps(runs, indent=1))


if __name__ == "__main__":
    main()
