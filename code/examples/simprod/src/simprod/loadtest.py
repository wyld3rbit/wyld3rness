"""The load test: does simprod fit in the container VM, and at what rate does it run?

    make load-test          (from code/; needs `make up`)

Research log entry 006 is the protocol; the constants below are its numbers, so
the entry and the code cannot disagree. Two parts:

    ramp   RAMP_RATES, RAMP_SECONDS each, ledger capacity set far above the load
           ──► C = the highest rate that passes (no failures, p99 ≤ P99_BAR_MS,
               VM CPU ≤ RAMP_CPU_BAR, late sends ≤ LATE_BAR); the ramp stops at
               the first rate that fails. If every rate passes, C is a lower bound.
    rate   R = the highest ramp rate with STORM_FACTOR x R ≤ C, so a retry storm
           (each request sent up to 1 + 3 times) still fits in what the VM can serve
    soak   SOAK_REPEATS x SOAK_SECONDS at R, ledger capacity 2 x R (50% busy)
           ──► each must pass: no failures, p99 ≤ P99_BAR_MS, VM CPU ≤ SOAK_CPU_BAR,
               VM memory available ≥ MEM_FLOOR_MIB at its lowest

In the ramp the VM is the bottleneck, by design: the ledger is given 64 slots of
1 ms per shard, so whatever saturates first is CPU, threads or the network stack,
which is the question. In the soak the ledger is set as it will run in the
storms, one slot per shard holding each write for 2000 / R ms.

What is measured where. Customer outcomes and latency come from the load
generator (loadgen.py). VM-wide CPU and memory come from /proc/stat and
/proc/meminfo read inside a container, which show the whole VM, not the
container. Per-container CPU comes from `docker stats`, for attribution only.
Langfuse and Postgres are expected to be running: the storms run beside them.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from typing import Any

JSON = dict[str, Any]

RAMP_RATES = (25, 50, 100, 200, 400, 800, 1600)
RAMP_SECONDS = 60
RAMP_CPU_BAR = 80.0  # percent of the whole VM (all CPUs)
GATEWAY_RETRIES = 3
STORM_FACTOR = 1 + GATEWAY_RETRIES  # a storm sends each request at most this many times
# Pinned for every phase, so a value exported in the shell cannot change what is measured.
PINNED = {"GATEWAY_RETRIES": str(GATEWAY_RETRIES), "GATEWAY_TIMEOUT_MS": "500", "GATEWAY_BACKOFF": "0"}
SOAK_SECONDS = 300
SOAK_REPEATS = 3
SOAK_CPU_BAR = 60.0
P99_BAR_MS = 250.0  # half the gateway's 500 ms timeout: a healthy system never times out
MEM_FLOOR_MIB = 1024
LATE_BAR = 0.01  # share of sends more than 100 ms behind schedule: above it, the generator was the bottleneck
WARMUP_S = 5  # samples in the first seconds of a phase are left out of the CPU mean
SAMPLE_EVERY_S = 2.0
MIN_SAMPLE_SHARE = 0.5  # a phase with fewer VM samples than half those expected has no CPU measurement

ROOT = Path(__file__).resolve().parents[2]  # examples/simprod
COMPOSE = ROOT / "docker-compose.yml"
RESULTS = ROOT / "results"
RAMP_LEDGER = {"LEDGER_WORKERS": "64", "LEDGER_SERVICE_MS": "1"}


# ---- Pure parts: parsing, decisions ---------------------------------------------------


def cpu_ticks(stat: str) -> tuple[int, int]:
    """(busy, total) jiffies from the first line of /proc/stat. Idle and iowait count as not busy."""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            fields = [int(x) for x in line.split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            total = sum(fields[:8])  # user..steal; guest time is already inside user
            return total - idle, total
    raise ValueError("no cpu line in /proc/stat")


def cpu_percent(before: tuple[int, int], after: tuple[int, int]) -> float:
    """VM-wide CPU use between two samples, 0-100 over all CPUs."""
    busy, total = after[0] - before[0], after[1] - before[1]
    return round(100.0 * busy / total, 1) if total > 0 else 0.0


def mem_available_mib(meminfo: str) -> int:
    """MemAvailable from /proc/meminfo, in MiB."""
    for line in meminfo.splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise ValueError("no MemAvailable in /proc/meminfo")


def passes(phase: Mapping[str, Any], cpu_bar: float) -> list[str]:
    """The reasons a phase fails its bar; empty when it passes."""
    why = []
    late = phase["load"].get("late_sends", 0) / max(1, phase["load"].get("sent", 1))
    if late > LATE_BAR:
        why.append(f"{late:.1%} late sends > {LATE_BAR:.0%}: the generator could not keep up")
    if phase["load"]["failed"]:
        why.append(f"{phase['load']['failed']} failed requests")
    p99 = phase["load"]["latency_ms"]["p99"]
    if p99 is None or p99 > P99_BAR_MS:
        why.append(f"p99 {p99} ms > {P99_BAR_MS:g}")
    expected = max(1, int((phase["seconds"] - WARMUP_S) / SAMPLE_EVERY_S))
    if phase["vm"]["cpu_samples"] < MIN_SAMPLE_SHARE * expected:
        why.append(f"VM CPU not measured: {phase['vm']['cpu_samples']} of {expected} samples")
    elif phase["vm"]["cpu_mean"] > cpu_bar:
        why.append(f"VM CPU {phase['vm']['cpu_mean']}% > {cpu_bar:g}%")
    return why


def capacity(ramp: Sequence[Mapping[str, Any]]) -> int | None:
    """C: the highest ramp rate that passed, counting only up to the first failure."""
    best = None
    for phase in ramp:
        if passes(phase, RAMP_CPU_BAR):
            break
        best = phase["rate"]
    return best


def operating_rate(c: int | None) -> int | None:
    """R: the highest ramp rate whose storm load fits under C. None means stop and resize."""
    if c is None:
        return None
    fitting = [r for r in RAMP_RATES if STORM_FACTOR * r <= c]
    return max(fitting) if fitting else None


def soak_ledger(rate: int) -> dict[str, str]:
    """Ledger settings for capacity 2 x rate over 4 shards: one slot, 2000 / rate ms per write."""
    return {"LEDGER_WORKERS": "1", "LEDGER_SERVICE_MS": f"{2000 / rate:g}"}


def soak_verdict(phase: Mapping[str, Any]) -> list[str]:
    """The soak bar: the ramp's conditions with the stricter CPU bar, plus the memory floor."""
    why = passes(phase, SOAK_CPU_BAR)
    if phase["vm"]["mem_min_mib"] < MEM_FLOOR_MIB:
        why.append(f"VM memory available fell to {phase['vm']['mem_min_mib']} MiB < {MEM_FLOOR_MIB}")
    return why


# ---- Docker -----------------------------------------------------------------------------


def compose(*args: str, env: Mapping[str, str] | None = None, capture: bool = False) -> str:
    """Run `docker compose` on simprod's file. Raises on failure."""
    done = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), *args],
        env={**os.environ, **(env or {})},
        capture_output=capture,
        text=True,
        check=True,
    )
    return done.stdout if capture else ""


def docker(*args: str) -> str:
    """Run `docker` and return its stdout."""
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True).stdout


class Sampler:
    """Reads VM-wide CPU and memory every SAMPLE_EVERY_S from inside a container, and docker stats every 10 s.

    A failed read is recorded and the loop goes on; it never ends a thread
    quietly. `passes` then refuses a phase with too few samples, so a phase can
    never pass its CPU bar on a measurement that was not taken.
    """

    def __init__(self, container: str) -> None:
        self.container = container
        self.samples: list[tuple[float, float, int]] = []  # (seconds into phase, cpu %, mem available MiB)
        self.stats: list[JSON] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def _read(self) -> tuple[tuple[int, int], int]:
        """One read of the VM's CPU ticks and available memory."""
        out = docker("exec", self.container, "cat", "/proc/stat", "/proc/meminfo")
        return cpu_ticks(out), mem_available_mib(out)

    def _vm_loop(self, start: float) -> None:
        """CPU between consecutive reads, and memory at each read, until stopped."""
        before: tuple[int, int] | None = None
        while True:
            try:
                after, mem = self._read()
                if before is not None:
                    self.samples.append((round(time.monotonic() - start, 1), cpu_percent(before, after), mem))
                before = after
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                self.errors.append(f"vm read: {type(exc).__name__}: {exc}")
                before = None  # a gap: the next interval starts at the next good read
            if self._stop.wait(SAMPLE_EVERY_S):
                return

    def _stats_loop(self, start: float) -> None:
        """Per-container CPU and memory from docker stats, for attribution only."""
        while not self._stop.wait(10):
            try:
                out = docker("stats", "--no-stream", "--format", "{{json .}}")
                at = round(time.monotonic() - start, 1)
                for line in out.splitlines():
                    s = json.loads(line)
                    self.stats.append({"t": at, "name": s["Name"], "cpu": s["CPUPerc"], "mem": s["MemUsage"]})
            except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                self.errors.append(f"docker stats: {type(exc).__name__}: {exc}")

    def __enter__(self) -> Sampler:
        """Start both sampling threads."""
        start = time.monotonic()
        self._threads = [
            threading.Thread(target=f, args=(start,), daemon=True) for f in (self._vm_loop, self._stats_loop)
        ]
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Stop sampling and wait for the threads."""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=15)

    def summary(self) -> JSON:
        """CPU mean and max after warm-up, memory at its lowest, every sample and every error."""
        cpu = [c for t, c, _ in self.samples if t >= WARMUP_S]
        return {
            "cpu_samples": len(cpu),
            "cpu_mean": round(statistics.fmean(cpu), 1) if cpu else None,
            "cpu_max": max(cpu) if cpu else None,
            "mem_min_mib": min((m for _, _, m in self.samples), default=0),
            "samples": [{"t": t, "cpu": c, "mem_mib": m} for t, c, m in self.samples],
            "docker_stats": self.stats,
            "errors": self.errors,
        }


def phase(name: str, rate: int, seconds: int, seed: int, ledger: Mapping[str, str]) -> JSON:
    """Recreate the services with these ledger settings, run the load, sample the VM. One phase."""
    env = {**PINNED, **ledger}
    compose("up", "-d", "--wait", "--force-recreate", "ledger", "notifications", "gateway", env=env)
    edge = compose("ps", "-q", "edge", capture=True).strip()
    if not edge:
        raise RuntimeError("the edge container is not running: run `make sim-up` first")
    print(f"[{datetime.now(UTC):%H:%M:%S}] {name}: {rate}/s for {seconds} s, ledger {dict(ledger)}", flush=True)
    with Sampler(edge) as sampler:
        out = compose(
            "run", "--rm", "-T", "loadgen", "python", "-m", "simprod.loadgen",
            "--rate", str(rate), "--seconds", str(seconds), "--seed", str(seed),
            env=env, capture=True,
        )  # fmt: skip
    line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("RESULT ")), None)
    if line is None:
        raise RuntimeError(f"{name}: the load generator printed no RESULT line")
    result = {
        "name": name,
        "rate": rate,
        "seconds": seconds,
        "seed": seed,
        "ledger": dict(ledger),
        "load": json.loads(line[len("RESULT ") :]),
        "vm": sampler.summary(),
    }
    load, vm = result["load"], result["vm"]
    print(
        f"    ok {load['outcomes'].get('ok', 0)}/{load['sent']}  failed {load['failed']}  late {load['late_sends']}  "
        f"p50 {load['latency_ms']['p50']} ms  p99 {load['latency_ms']['p99']} ms  "
        f"VM CPU mean {vm['cpu_mean']}% max {vm['cpu_max']}% ({vm['cpu_samples']} samples)  "
        f"mem min {vm['mem_min_mib']} MiB" + (f"  sampler errors {len(vm['errors'])}" if vm["errors"] else ""),
        flush=True,
    )
    return result


def conditions() -> JSON:
    """What else was running, and the VM's size: the context any number here depends on."""
    info = json.loads(docker("info", "--format", "{{json .}}"))
    return {
        "containers": sorted(docker("ps", "--format", "{{.Names}}").split()),
        "vm_cpus": info.get("NCPU"),
        "vm_mem_mib": int(info.get("MemTotal", 0)) // 2**20,
        "image": docker("image", "inspect", "simprod:local", "--format", "{{.Id}}").strip(),
        "commit": subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, cwd=ROOT
        ).stdout.strip(),
    }


def main(argv: list[str] | None = None) -> int:
    """Run the ramp, choose R, run the soaks, print the verdict, write the results JSON."""
    parser = argparse.ArgumentParser(prog="simprod-loadtest", description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="results file (default: results/loadtest-<UTC>.json)")
    args = parser.parse_args(argv)
    started = datetime.now(UTC)
    out = args.out or RESULTS / f"loadtest-{started:%Y%m%dT%H%M%SZ}.json"
    protocol = {
        "ramp_rates": RAMP_RATES,
        "ramp_seconds": RAMP_SECONDS,
        "ramp_cpu_bar": RAMP_CPU_BAR,
        "storm_factor": STORM_FACTOR,
        "pinned": PINNED,
        "min_sample_share": MIN_SAMPLE_SHARE,
        "soak_seconds": SOAK_SECONDS,
        "soak_repeats": SOAK_REPEATS,
        "soak_cpu_bar": SOAK_CPU_BAR,
        "p99_bar_ms": P99_BAR_MS,
        "mem_floor_mib": MEM_FLOOR_MIB,
        "late_bar": LATE_BAR,
    }
    report: JSON = {
        "started_utc": started.isoformat(timespec="seconds"),
        "protocol": protocol,
        "conditions": conditions(),
        "ramp": [],
        "soak": [],
    }
    print(f"conditions: {report['conditions']}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)

    def save() -> None:
        """Write what has been measured so far: a failure later never loses an earlier phase."""
        out.write_text(json.dumps(report, indent=1) + "\n")

    try:
        _run(report, save)
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["pass"] = False
        raise
    finally:
        report["ended_utc"] = datetime.now(UTC).isoformat(timespec="seconds")
        save()
        restore()
        print(f"results: {out}", flush=True)
    return 0 if report["pass"] else 1


def _run(report: JSON, save: Callable[[], None]) -> None:
    """The ramp, the choice of R, the soaks and the verdict table, saving after every phase."""
    for rate in RAMP_RATES:
        result = phase("ramp", rate, RAMP_SECONDS, seed=rate, ledger=RAMP_LEDGER)
        result["fails"] = passes(result, RAMP_CPU_BAR)
        report["ramp"].append(result)
        save()
        if result["fails"]:
            print(f"    knee: {'; '.join(result['fails'])}", flush=True)
            break
    c = capacity(report["ramp"])
    r = operating_rate(c)
    censored = len(report["ramp"]) == len(RAMP_RATES) and not report["ramp"][-1]["fails"]
    report["capacity"], report["capacity_is_lower_bound"], report["operating_rate"] = c, censored, r
    shown = f"at least {c}" if censored else f"{c}"
    print(f"C = {shown}/s, R = {r}/s (storm factor {STORM_FACTOR})", flush=True)

    if r is not None:
        for seed in range(1, SOAK_REPEATS + 1):
            result = phase(f"soak {seed}", r, SOAK_SECONDS, seed=seed, ledger=soak_ledger(r))
            result["fails"] = soak_verdict(result)
            report["soak"].append(result)
            save()
    report["pass"] = r is not None and all(not s["fails"] for s in report["soak"])

    print("\nphase      rate  failed  late  p50 ms  p99 ms  VM CPU mean/max  mem min MiB  verdict")
    for p in report["ramp"] + report["soak"]:
        lat, vm, load = p["load"]["latency_ms"], p["vm"], p["load"]
        print(
            f"{p['name']:<9} {p['rate']:>5}  {load['failed']:>6}  {load['late_sends']:>4}  {lat['p50']!s:>6}  "
            f"{lat['p99']!s:>6}  {vm['cpu_mean']!s:>6}/{vm['cpu_max']!s:<8}  {vm['mem_min_mib']:>11}  "
            f"{'; '.join(p['fails']) or 'pass'}"
        )
    print(f"\nverdict: {'PASS' if report['pass'] else 'FAIL'}  (C = {shown}/s, R = {r}/s)", flush=True)


def restore() -> None:
    """Put the services back on the compose defaults, so inspection afterwards sees the documented system."""
    try:
        compose("up", "-d", "--wait", "--force-recreate", "ledger", "notifications", "gateway")
    except subprocess.CalledProcessError as exc:
        print(f"could not restore the default settings ({exc}): run `make sim-up`", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
