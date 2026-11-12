#!/usr/bin/env python3
"""Run ON a Linux NVIDIA machine: CPU cost of one tick's GPU reading, nvidia-smi (two processes) vs NVML (ctypes).

    python3 nvml_vs_smi.py [rounds]

Alternates the two, 5 rounds of 10 readings each. CPU = this process's own CPU (NVML) or its children's (nvidia-smi),
from os.times(); wall = elapsed. Checks both report the same GPUs and roughly the same memory used.
"""
import ctypes
import os
import statistics as st
import subprocess
import sys
import time

Q = ("index,name,uuid,temperature.gpu,utilization.gpu,utilization.memory,memory.used,memory.total,power.draw,"
     "power.limit,fan.speed,clocks.sm,clocks.mem,pstate,pcie.link.gen.current,pcie.link.width.current,"
     "pcie.link.gen.max,pcie.link.width.max,driver_version,temperature.memory,clocks_throttle_reasons.active")


def smi():
    a = subprocess.run(["nvidia-smi", f"--query-gpu={Q}", "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True)
    subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory,gpu_uuid", "--format=csv,noheader,nounits"],
                   capture_output=True, text=True, check=True)
    return [(int(r.split(",")[0]), float(r.split(",")[6])) for r in a.stdout.strip().splitlines()]


class Mem(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class Util(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class Proc(ctypes.Structure):      # nvmlProcessInfo_v2
    _fields_ = [("pid", ctypes.c_uint), ("used", ctypes.c_ulonglong), ("gi", ctypes.c_uint), ("ci", ctypes.c_uint)]


N = ctypes.CDLL("libnvidia-ml.so.1")
assert N.nvmlInit_v2() == 0


def nvml():
    """The same tick through the library: per GPU name, temp, utilisation, memory, power, limit, fan, clocks,
    pstate, PCIe, throttle reasons, and the compute processes."""
    n = ctypes.c_uint(); N.nvmlDeviceGetCount_v2(ctypes.byref(n)); out = []
    for i in range(n.value):
        h = ctypes.c_void_p(); N.nvmlDeviceGetHandleByIndex_v2(i, ctypes.byref(h))
        name = ctypes.create_string_buffer(96); N.nvmlDeviceGetName(h, name, 96)
        uuid = ctypes.create_string_buffer(96); N.nvmlDeviceGetUUID(h, uuid, 96)
        v = ctypes.c_uint(); m = Mem(); u = Util(); ul = ctypes.c_ulonglong()
        N.nvmlDeviceGetTemperature(h, 0, ctypes.byref(v)); N.nvmlDeviceGetUtilizationRates(h, ctypes.byref(u))
        N.nvmlDeviceGetMemoryInfo(h, ctypes.byref(m)); N.nvmlDeviceGetPowerUsage(h, ctypes.byref(v))
        N.nvmlDeviceGetEnforcedPowerLimit(h, ctypes.byref(v)); N.nvmlDeviceGetFanSpeed(h, ctypes.byref(v))
        for clk in (1, 2):
            N.nvmlDeviceGetClockInfo(h, clk, ctypes.byref(v))
        N.nvmlDeviceGetPerformanceState(h, ctypes.byref(v)); N.nvmlDeviceGetCurrPcieLinkGeneration(h, ctypes.byref(v))
        N.nvmlDeviceGetCurrPcieLinkWidth(h, ctypes.byref(v)); N.nvmlDeviceGetMaxPcieLinkGeneration(h, ctypes.byref(v))
        N.nvmlDeviceGetMaxPcieLinkWidth(h, ctypes.byref(v)); N.nvmlDeviceGetCurrentClocksThrottleReasons(h, ctypes.byref(ul))
        cnt = ctypes.c_uint(64); procs = (Proc * 64)(); N.nvmlDeviceGetComputeRunningProcesses_v2(h, ctypes.byref(cnt), procs)
        out.append((i, m.used / 2 ** 20))
    return out


def measure(fn, k=10):
    t0, w0 = os.times(), time.perf_counter()
    for _ in range(k):
        r = fn()
    t1, w1 = os.times(), time.perf_counter()
    cpu = (t1.user - t0.user) + (t1.system - t0.system) + (t1.children_user - t0.children_user) + (t1.children_system - t0.children_system)
    return cpu / k * 1000, (w1 - w0) / k * 1000, r


rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 5
res = {"nvidia-smi x2": [], "NVML ctypes": []}
for r in range(rounds):
    for name, fn in (("nvidia-smi x2", smi), ("NVML ctypes", nvml)):
        cpu, wall, out = measure(fn)
        res[name].append((cpu, wall, out))
a, b = res["nvidia-smi x2"][-1][2], res["NVML ctypes"][-1][2]
agree = len(a) == len(b) and all(x[0] == y[0] and abs(x[1] - y[1]) < 600 for x, y in zip(a, b))
for name, rs in res.items():
    c = [x[0] for x in rs]; w = [x[1] for x in rs]
    print(f"{name:14} CPU {st.median(c):7.2f} ms [{min(c):.2f}-{max(c):.2f}]  wall {st.median(w):7.2f} ms [{min(w):.2f}-{max(w):.2f}]  per reading, {rounds} rounds x 10")
print("same GPUs and memory used (within 600 MiB):", agree, a, b)
