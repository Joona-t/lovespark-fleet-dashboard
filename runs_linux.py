"""Runs probe for a Linux GPU box: what the machine is running, as one JSON line every INTERVAL seconds.

Piped over SSH into python3 like agent_linux.py (nothing is installed on the machine). The server prepends a
CONFIG line built from the device's "runs" entry in devices.json; the defaults below apply to anything missing.

    python3 -u - <interval> <max_life>   < runs_linux.py

Each line holds:
  vllm   the vLLM server's live numbers from its /metrics: requests running and waiting, KV cache use, token counters
  lock   who holds the GPU lock file (a flock), if one is configured
  jobs   processes whose command line matches the job pattern: pid, age, command, working folder
  runs   log files written in the last day under the run folders: path, age, size, last lines (newest first)
Paths under the home folder are written as ~/..., so no home path leaves the machine.
"""
import json
import os
import re
import sys
import time
import urllib.request

DEFAULTS = {
    "vllm": "http://127.0.0.1:8000",       # base URL of the vLLM server, or null
    "lock": None,                          # e.g. "~/.gpu.lock"
    "roots": ["~"],                        # folders searched for run logs
    "exclude": [".venv", "venv", ".git", "node_modules", "__pycache__", ".cache", "site-packages", "/vllm/logs"],
    "suffixes": [".log", ".jsonl"],
    "jobs": r"bench\.runner|run_arm|evals?/run|run[-_]eval|llama-bench|pytest|train",   # regex on the command line
    "max_runs": 12,
    "max_age": 86400,                      # seconds: older logs are not listed
}
try:
    CONFIG                                 # noqa: F821  (prepended by the server)
except NameError:
    CONFIG = {}
CFG = dict(DEFAULTS, **{k: v for k, v in CONFIG.items() if k in DEFAULTS})
HOME = os.path.expanduser("~")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
SCAN_EVERY = 30                            # seconds between walks of the run folders (the costly part)


def tilde(path):
    """A path with the home folder written as ~."""
    path = str(path)
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def parse_metrics(text):
    """The vLLM numbers we show, from Prometheus text. Sums over engines; ignores labels."""
    want = {"vllm:num_requests_running": "running", "vllm:num_requests_waiting": "waiting",
            "vllm:kv_cache_usage_perc": "kv", "vllm:generation_tokens_total": "gen_total",
            "vllm:prompt_tokens_total": "prompt_total"}
    out, model = {}, None
    for line in text.splitlines():
        if not line.startswith("vllm:"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        if name not in want:
            continue
        m = re.search(r'model_name="([^"]*)"', line)
        model = model or (m.group(1) if m else None)
        try:
            val = float(line.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            continue
        out[want[name]] = out.get(want[name], 0.0) + val
    if "kv" in out:
        out["kv"] = round(out["kv"] * 100, 1)
    out["model"] = model
    return out


def vllm():
    if not CFG["vllm"]:
        return None
    try:
        with urllib.request.urlopen(CFG["vllm"].rstrip("/") + "/metrics", timeout=2) as r:
            return dict(parse_metrics(r.read().decode("utf-8", "replace")), up=True)
    except Exception:
        return {"up": False}


def boot_time():
    try:
        for line in open("/proc/stat"):
            if line.startswith("btime"):
                return int(line.split()[1])
    except OSError:                       # not Linux (the tests import this module on a Mac)
        pass
    return 0


TICKS = os.sysconf("SC_CLK_TCK")
BOOT = boot_time()


def proc_info(pid):
    """(command, working folder, age in seconds) of a process, or None if it is gone or not ours."""
    try:
        raw = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        stat = open(f"/proc/{pid}/stat").read()
        start = int(stat[stat.rindex(")") + 2:].split()[19]) / TICKS
        try:
            cwd = tilde(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            cwd = None
        return re.sub(re.escape(HOME), "~", raw)[:200], cwd, round(time.time() - (BOOT + start))
    except (OSError, ValueError, IndexError):
        return None


def lock_holder():
    """The process holding the configured lock file, from /proc/locks."""
    if not CFG["lock"]:
        return None
    path = os.path.expanduser(CFG["lock"])
    try:
        st = os.stat(path)
    except OSError:
        return {"path": tilde(path), "held": False}
    key = f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"
    for line in open("/proc/locks"):
        f = line.split()
        if "->" in f:
            continue
        if len(f) >= 6 and f[5] == key:
            info = proc_info(f[4])
            return {"path": tilde(path), "held": True, "pid": int(f[4]),
                    "cmd": info[0] if info else None, "age": info[2] if info else None}
    return {"path": tilde(path), "held": False}


def jobs():
    pat, me, out = re.compile(CFG["jobs"]), os.getpid(), []
    for name in os.listdir("/proc"):
        if not name.isdigit() or int(name) == me:
            continue
        info = proc_info(name)
        if not info or not info[0] or not pat.search(info[0]):
            continue
        out.append({"pid": int(name), "cmd": info[0], "cwd": info[1], "age": info[2]})
    out.sort(key=lambda j: -j["age"])
    seen, uniq = set(), []
    for j in out:                                     # a shell wrapper and its subshells share one command line
        if (j["cmd"], j["cwd"]) not in seen:
            seen.add((j["cmd"], j["cwd"]))
            uniq.append(j)
    return uniq[:20]


def tail(path, n=3):
    """The last n non-empty lines of a file, ANSI codes removed, each capped at 160 characters."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 8192))
            lines = f.read().decode("utf-8", "replace").split("\n")
    except OSError:
        return []
    lines = [ANSI.sub("", l.rsplit("\r", 1)[-1]).strip() for l in lines]     # a progress bar shows its last state
    return [l[:160] for l in lines if l][-n:]


def scan():
    """Log files written in the last max_age seconds under the run folders, newest first."""
    now, found = time.time(), []
    skip = CFG["exclude"]
    for root in CFG["roots"]:
        base = os.path.expanduser(root)
        for dirpath, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip
                       and not any(s in os.path.join(dirpath, d) for s in skip if "/" in s)]
            if dirpath[len(base):].count(os.sep) >= 6:
                dirs[:] = []
            for fn in files:
                if not fn.endswith(tuple(CFG["suffixes"])):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if now - st.st_mtime <= CFG["max_age"]:
                    found.append((st.st_mtime, st.st_size, p))
    found.sort(reverse=True)
    return found[:CFG["max_runs"]]


def main():
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 5
    life = float(sys.argv[2]) if len(sys.argv) > 2 else 600
    start, files, scanned = time.time(), [], 0.0
    while time.time() - start < life:
        t0 = time.time()
        if t0 - scanned >= SCAN_EVERY:
            files, scanned = scan(), t0
        runs = []
        for _, _, p in files:
            try:
                st = os.stat(p)                       # sizes and ages are fresh every tick; the list every 30 s
            except OSError:
                continue
            runs.append({"path": tilde(p), "age": round(t0 - st.st_mtime), "size": st.st_size, "tail": tail(p)})
        line = {"t": t0, "vllm": vllm(), "lock": lock_holder(), "jobs": jobs(), "runs": runs,
                "cost_ms": None}
        line["cost_ms"] = round((time.time() - t0) * 1000, 1)
        try:
            sys.stdout.write(json.dumps(line, separators=(",", ":")) + "\n")
            sys.stdout.flush()
        except BrokenPipeError:
            return
        if os.getppid() == 1:                         # our SSH session is gone
            return
        time.sleep(max(0.2, interval - (time.time() - t0)))


if __name__ == "__main__":
    main()
