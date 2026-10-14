"""Read-only host performance reader: CPU %, memory %, CPU MHz, CPU temperature, network KB/s.

READ-ONLY: this module never writes anything, starts no subprocess, uses no network and no
shell. It only reads text files under two injectable roots (defaults `/proc` and `/sys`),
standard library only.

Sources:
  CPU %        first line of <proc>/stat; busy = 1 - (idle + iowait) / total over the first
               eight fields (guest fields are already inside user/nice); needs two samples.
  memory %     <proc>/meminfo: (MemTotal - MemAvailable) / MemTotal.
  CPU MHz      mean of <sys>/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq (kHz), half-up.
  CPU temp     <sys>/class/hwmon/hwmon*/ whose `name` is `k10temp`, the `temp*_input` whose
               `temp*_label` is `Tctl` (found by name: hwmon indexes change between boots).
               Millidegrees to whole degrees: (millideg + 500) // 1000, i.e. half up.
  network      <proc>/net/dev RX/TX bytes of the configured interfaces, or of the default-route
               interface from <proc>/net/route (lowest metric); needs two samples.

A missing or unreadable source makes the whole call return None; no value is ever invented
(0 included). Never raises. One WARNING per source per outage, one INFO on recovery.

INFERRED, not confirmed by the device: the units (percent, MHz, degrees C, KB/s) come from the
vendor theme's default label texts and the SDK, not from the device.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path

log = logging.getLogger("mass80d.perf_reader")

INT32_MAX = 2_147_483_647
_DEFAULT_ROUTE = "00000000"
_STAT_FIELDS = 8  # user nice system idle iowait irq softirq steal
_TEMP_DEVICE = "k10temp"
_TEMP_LABEL = "Tctl"

PerfTuple = tuple[int, int, int, int, int, int]


class _SourceError(Exception):
    """A source could not be read or parsed (message goes to the log)."""


def _clamp(value: int, high: int = INT32_MAX) -> int:
    return max(0, min(high, value))


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, ValueError) as exc:  # ValueError covers UnicodeDecodeError
        raise _SourceError(f"cannot read {path.name}: {type(exc).__name__}") from exc


def default_route_interface(proc_root: str) -> str | None:
    """Interface of the default route (Destination and Mask 00000000), lowest Metric, or None."""
    try:
        text = (Path(proc_root) / "net" / "route").read_text()
    except OSError, ValueError:
        return None
    best: tuple[int, str] | None = None
    for row in text.splitlines()[1:]:
        parts = row.split()
        if len(parts) < 8:
            continue
        iface, dest, metric_s, mask = parts[0], parts[1], parts[6], parts[7]
        if dest != _DEFAULT_ROUTE or mask != _DEFAULT_ROUTE:
            continue
        try:
            metric = int(metric_s)
        except ValueError:
            continue
        if best is None or metric < best[0]:
            best = (metric, iface)
    return None if best is None else best[1]


class PerfReader:
    """Callable returning (memory, cpu, freq_mhz, temp_c, net_up_kbs, net_down_kbs) or None."""

    def __init__(
        self,
        *,
        proc_root: str = "/proc",
        sys_root: str = "/sys",
        interfaces: Sequence[str] = (),
        clock: Callable[[], float] = time.monotonic,
        cache_s: float = 2.0,
    ) -> None:
        self._proc = Path(proc_root)
        self._sys = Path(sys_root)
        self._interfaces = tuple(interfaces)
        self._clock = clock
        self._cache_s = cache_s
        self._last: PerfTuple | None = None
        self._last_at = 0.0
        self._cpu_prev: tuple[int, int] | None = None  # (idle, total)
        self._net_prev: dict[str, tuple[int, int]] | None = None
        self._net_prev_at = 0.0
        self._temp_path: tuple[Path, int] | None = None  # (hwmon dir, temp index)
        self._failed: set[str] = set()

    def __call__(self) -> PerfTuple | None:
        try:
            return self._read_all()
        except Exception:  # last resort: the provider contract is "never raises"
            log.warning("performance read failed unexpectedly", exc_info=True)
            return None

    # --- orchestration -------------------------------------------------------------------

    def _read_all(self) -> PerfTuple | None:
        now = self._clock()
        if self._last is not None and 0 <= now - self._last_at < self._cache_s:
            return self._last
        # Every source runs (no short-circuit) so each keeps its baseline and its log state.
        memory = self._source("memory", self._memory)
        cpu = self._source("cpu", lambda: self._cpu())
        freq = self._source("frequency", self._frequency)
        temp = self._source("temperature", self._temperature)
        net = self._source("network", lambda: self._network(now))
        if memory is None or cpu is None or freq is None or temp is None or net is None:
            return None
        result = (memory, cpu, freq, temp, net[0], net[1])
        self._last = result
        self._last_at = now
        return result

    def _source[T](self, name: str, fn: Callable[[], T | None]) -> T | None:
        try:
            value = fn()
        except _SourceError as exc:
            if name not in self._failed:
                self._failed.add(name)
                log.warning("%s unavailable: %s", name, exc)
            return None
        except (OSError, ValueError, IndexError, ArithmeticError) as exc:
            if name not in self._failed:
                self._failed.add(name)
                log.warning("%s unavailable: %s", name, type(exc).__name__)
            return None
        if name in self._failed:
            self._failed.discard(name)
            log.info("%s recovered", name)
        return value

    # --- sources -------------------------------------------------------------------------

    def _cpu(self) -> int | None:
        line = _read(self._proc / "stat").split("\n", 1)[0].split()
        if not line or line[0] != "cpu" or len(line) < 1 + _STAT_FIELDS:
            raise _SourceError("unexpected /proc/stat first line")
        try:
            nums = [int(x) for x in line[1 : 1 + _STAT_FIELDS]]
        except ValueError as exc:
            raise _SourceError("non-numeric /proc/stat field") from exc
        sample = (nums[3] + nums[4], sum(nums))
        prev = self._cpu_prev
        if prev is None:
            self._cpu_prev = sample  # baseline: no value yet, and not an outage
            return None
        d_idle = sample[0] - prev[0]
        d_total = sample[1] - prev[1]
        if d_total <= 0:
            return None  # keep the old sample
        self._cpu_prev = sample
        return _clamp(round(100 * (1 - d_idle / d_total)), 100)

    def _memory(self) -> int:
        values: dict[str, int] = {}
        for row in _read(self._proc / "meminfo").splitlines():
            key, sep, rest = row.partition(":")
            if sep and key in ("MemTotal", "MemAvailable"):
                parts = rest.split()
                if not parts:
                    raise _SourceError(f"empty {key}")
                values[key] = int(parts[0])  # ValueError handled by _source
        if "MemTotal" not in values or "MemAvailable" not in values:
            raise _SourceError("MemTotal or MemAvailable missing")
        total = values["MemTotal"]
        if total <= 0:
            raise _SourceError("MemTotal is not positive")
        return _clamp(round(100 * (total - values["MemAvailable"]) / total), 100)

    def _frequency(self) -> int:
        cpu_dir = self._sys / "devices" / "system" / "cpu"
        try:
            dirs = sorted(cpu_dir.glob("cpu[0-9]*"))
        except OSError as exc:
            raise _SourceError("cannot list cpu directories") from exc
        total_khz = 0
        count = 0
        for d in dirs:
            try:
                khz = int((d / "cpufreq" / "scaling_cur_freq").read_text().strip())
            except OSError, ValueError:
                continue
            if khz < 0:
                continue
            total_khz += khz
            count += 1
        if count == 0:
            raise _SourceError("no readable scaling_cur_freq")
        return _clamp((total_khz + 500 * count) // (1000 * count))

    def _read_temp_at(self, hwmon: Path, index: int) -> int | None:
        """Millidegrees from a known place, or None if it no longer matches."""
        try:
            if hwmon.joinpath("name").read_text().strip() != _TEMP_DEVICE:
                return None
            if hwmon.joinpath(f"temp{index}_label").read_text().strip() != _TEMP_LABEL:
                return None
            return int(hwmon.joinpath(f"temp{index}_input").read_text().strip())
        except OSError, ValueError:
            return None

    def _temperature(self) -> int:
        if self._temp_path is not None:
            milli = self._read_temp_at(*self._temp_path)
            if milli is not None:
                return _clamp((milli + 500) // 1000)
            self._temp_path = None  # stopped working: discover again
        hwmon_root = self._sys / "class" / "hwmon"
        try:
            devices = sorted(hwmon_root.iterdir())
        except OSError as exc:
            raise _SourceError("cannot list hwmon devices") from exc
        for device in devices:
            try:
                if device.joinpath("name").read_text().strip() != _TEMP_DEVICE:
                    continue
                labels = sorted(device.glob("temp*_label"))
            except OSError, ValueError:
                continue
            for label_path in labels:
                digits = label_path.name[len("temp") : -len("_label")]
                if not digits.isdigit():
                    continue
                milli = self._read_temp_at(device, int(digits))
                if milli is not None:
                    self._temp_path = (device, int(digits))
                    return _clamp((milli + 500) // 1000)
        raise _SourceError(f"no {_TEMP_DEVICE} {_TEMP_LABEL} sensor")

    def _netdev(self) -> dict[str, tuple[int, int]]:
        """interface -> (rx_bytes, tx_bytes); rows that do not parse are skipped."""
        counters: dict[str, tuple[int, int]] = {}
        for row in _read(self._proc / "net" / "dev").splitlines()[2:]:
            name, sep, rest = row.partition(":")
            fields = rest.split()
            if not sep or len(fields) < 9:
                continue
            try:
                counters[name.strip()] = (int(fields[0]), int(fields[8]))
            except ValueError:
                continue
        return counters

    def _network(self, now: float) -> tuple[int, int] | None:
        names = self._interfaces or ()
        if not names:
            iface = default_route_interface(str(self._proc))
            if iface is None:
                raise _SourceError("no default route and no configured interface")
            names = (iface,)
        counters = self._netdev()
        missing = [n for n in names if n not in counters]
        if missing:
            raise _SourceError(f"interface not in /proc/net/dev: {missing[0]}")
        prev, prev_at = self._net_prev, self._net_prev_at
        if prev is None or any(n not in prev for n in names):
            self._net_prev, self._net_prev_at = counters, now  # baseline
            return None
        elapsed = now - prev_at
        if elapsed <= 0:
            return None  # keep the old sample
        self._net_prev, self._net_prev_at = counters, now
        rx = sum(max(0, counters[n][0] - prev[n][0]) for n in names)
        tx = sum(max(0, counters[n][1] - prev[n][1]) for n in names)
        return _clamp(int(tx / 1024 / elapsed)), _clamp(int(rx / 1024 / elapsed))
