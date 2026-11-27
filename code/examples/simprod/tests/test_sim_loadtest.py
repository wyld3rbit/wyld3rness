"""Tests for simprod/loadtest.py: the parsing and the pre-registered decision rules (entry 006)."""

from simprod import loadtest as lt

STAT = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 50 0 25 400 25 0 0 0 0 0\n"
STAT_LATER = "cpu  200 0 100 900 50 0 0 0 0 0\n"
MEMINFO = "MemTotal:        6053776 kB\nMemFree: 1 kB\nMemAvailable:    2876472 kB\n"


def phase(
    rate: int, failed: int = 0, p99: float | None = 40.0, cpu: float = 20.0, mem: int = 2000, samples: int = 27
) -> dict:
    return {
        "rate": rate,
        "seconds": 60,
        "load": {"failed": failed, "latency_ms": {"p99": p99}, "sent": 1000, "late_sends": 0},
        "vm": {"cpu_mean": cpu, "mem_min_mib": mem, "cpu_samples": samples},
    }


def test_cpu_is_busy_over_total_with_iowait_counted_idle() -> None:
    before, after = lt.cpu_ticks(STAT), lt.cpu_ticks(STAT_LATER)
    assert before == (150, 1000)  # idle 800 + iowait 50 are not busy
    assert lt.cpu_percent(before, after) == 60.0  # 150 more busy ticks of 250 more in total


def test_mem_available_in_mib() -> None:
    assert lt.mem_available_mib(MEMINFO) == 2809  # 2876472 kB / 1024, rounded down


def test_capacity_is_the_last_rate_before_the_first_failure() -> None:
    ramp = [phase(25), phase(50), phase(100), phase(200, cpu=85.0), phase(400)]
    assert lt.capacity(ramp) == 100  # 400 passing after a failure does not count
    assert lt.capacity([phase(25, failed=1)]) is None
    assert lt.capacity([phase(25), phase(50, p99=300.0)]) == 25


def test_the_operating_rate_leaves_room_for_a_fourfold_storm() -> None:
    assert lt.operating_rate(800) == 200
    assert lt.operating_rate(400) == 100
    assert lt.operating_rate(200) == 50
    assert lt.operating_rate(50) is None  # 4 x 25 = 100 > 50: stop and resize
    assert lt.operating_rate(None) is None


def test_the_soak_ledger_runs_at_half_capacity() -> None:
    assert lt.soak_ledger(100) == {"LEDGER_WORKERS": "1", "LEDGER_SERVICE_MS": "20"}
    assert lt.soak_ledger(25) == {"LEDGER_WORKERS": "1", "LEDGER_SERVICE_MS": "80"}


def test_the_soak_bar() -> None:
    assert lt.soak_verdict(phase(100)) == []
    assert lt.soak_verdict(phase(100, cpu=61.0)) == ["VM CPU 61.0% > 60%"]
    assert lt.soak_verdict(phase(100, mem=900)) == ["VM memory available fell to 900 MiB < 1024"]
    assert lt.soak_verdict(phase(100, p99=None)) == ["p99 None ms > 250"]


def test_a_generator_that_falls_behind_invalidates_the_phase() -> None:
    late = phase(800)
    late["load"].update(sent=1000, late_sends=20)
    assert lt.passes(late, lt.RAMP_CPU_BAR) == ["2.0% late sends > 1%: the generator could not keep up"]


def test_a_phase_whose_cpu_was_not_measured_fails() -> None:
    """No samples is not 0% CPU: the phase cannot pass a CPU bar it never measured."""
    unmeasured = phase(400, cpu=None, samples=0)  # type: ignore[arg-type]
    assert lt.passes(unmeasured, lt.RAMP_CPU_BAR) == ["VM CPU not measured: 0 of 27 samples"]
    assert lt.passes(phase(400, samples=14), lt.RAMP_CPU_BAR) == []  # half of 27, rounded up


def test_the_sampler_records_a_failed_read_and_keeps_going(monkeypatch) -> None:
    reads = iter([RuntimeError, STAT + MEMINFO, STAT_LATER + MEMINFO])

    def docker(*args: str) -> str:
        step = next(reads, STAT_LATER + MEMINFO)
        if step is RuntimeError:
            raise lt.subprocess.CalledProcessError(1, "docker exec")
        return step

    monkeypatch.setattr(lt, "docker", docker)
    monkeypatch.setattr(lt, "SAMPLE_EVERY_S", 0.01)
    monkeypatch.setattr(lt, "WARMUP_S", 0)
    with lt.Sampler("edge") as sampler:
        lt.time.sleep(0.1)
    s = sampler.summary()
    assert s["errors"] and "CalledProcessError" in s["errors"][0]
    assert s["cpu_samples"] >= 1 and s["cpu_mean"] is not None


def test_the_storm_factor_follows_the_pinned_retries() -> None:
    assert 1 + int(lt.PINNED["GATEWAY_RETRIES"]) == lt.STORM_FACTOR
