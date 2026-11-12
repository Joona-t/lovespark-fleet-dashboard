# Benchmarks: what watching a machine costs it

Measured in November 2026 on our own machines: a Linux box (Ryzen 9 9950X, 32 threads, RTX 3090 Ti + RTX 3090,
Ubuntu 26.04, driver 595.99), a Mac mini (M4) and a Windows PC (Ryzen 5 9600X, 12 threads, RTX 5060 Ti).

## Three designs (Linux box)

`bench/bench_linux.py <ssh-host>`: 5 rounds alternating the designs, 10 ticks of 3 s each. CPU is measured on the
watched machine: the commands' CPU (bash `times`) plus the sshd processes serving them (`/proc/<pid>/schedstat`).
0 errors in 150 ticks.

| Design | CPU per tick | Bytes per tick | Time to a reading |
|---|---|---|---|
| **stream** (this project): one SSH session, agent piped in | 56.1 ms [49.4–67.0] | 5,253 | 0.11 s |
| poll: a new SSH connection every 3 s | 67.7 ms [46.8–70.2] | 9,062 | 0.36 s |
| mux: the same poll over one reused connection | 57.2 ms [54.4–61.6] | 2,468 | 0.18 s |

SSH itself is ~1 ms a tick for a stream; a new connection per poll adds ~8 ms of sshd CPU and 3x the latency.
The poll probes threw their output away (no parsing, nothing sent back), so their numbers are lower bounds.

## The limiter: nvidia-smi

`bench/nvml_vs_smi.py` on the Linux box, 5 alternating rounds of 10 readings, the same fields both ways:

| GPU reading | CPU | Wall |
|---|---|---|
| two `nvidia-smi` launches | 45 ms [44–47] | 65 ms |
| NVML through `ctypes` | 2 ms [2–4] | 2.5 ms |

All 42 GPU fields matched (within 5% for values that move). Persistence mode was off, so every `nvidia-smi` launch also
initialised the driver.

## After the changes

| Machine | Before | After |
|---|---|---|
| Linux box | 56.1 ms CPU per tick | **16.4 ms** [16.2–16.9] (NVML; process table every third tick) |
| Mac mini | ~100 ms | **~61 ms** (IOHID sensors, `ps` and disk counters every third tick) |
| Windows PC | ~2.3 s CPU per minute with WMI | **~1.55 s** (no per-tick `Win32_OperatingSystem` query) |

Not done on Windows: a long-running `nvidia-smi -lms` would save ~20 ms a tick but would outlive the agent if
PowerShell were killed.

## Installed agents, as published (not measured here)

- Netdata: 1–5% of one core per node with defaults ([Netdata docs](https://learn.netdata.cloud/docs/netdata-agent/resource-utilization/cpu)).
- Beszel: ~3–23 MB of memory, under 0.1% CPU idle (third-party write-ups, e.g. [RamNode](https://ramnode.com/guides/beszel)).
