"""T27: PerfReader reads /proc and /sys. Every test uses fake trees in a TemporaryDirectory.

The one exception is RealHostSmokeTest, which is skipped when /proc/stat is missing and asserts
only types and ranges (it never prints or writes anything).
"""

from __future__ import annotations

import importlib
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mass80d import perf_reader
from mass80d.perf_reader import PerfReader, default_route_interface

LOGGER = "mass80d.perf_reader"
INT32_MAX = 2_147_483_647


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Tree:
    """A fake /proc and /sys under one TemporaryDirectory."""

    def __init__(self, root: str) -> None:
        self.proc = Path(root) / "proc"
        self.sys = Path(root) / "sys"
        (self.proc / "net").mkdir(parents=True)
        self.sys.mkdir()

    def _put(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def write_stat(self, user=0, nice=0, system=0, idle=0, iowait=0, irq=0, softirq=0,
                   steal=0, guest=0, guest_nice=0) -> None:  # fmt: skip
        nums = (user, nice, system, idle, iowait, irq, softirq, steal, guest, guest_nice)
        text = "cpu  " + " ".join(str(n) for n in nums) + "\ncpu0 1 2 3 4 5 6 7 8 9 10\n"
        self._put(self.proc / "stat", text + "intr 1 2 3\n")

    def write_meminfo(self, total=1000, available=250, extra="") -> None:
        lines = []
        if total is not None:
            lines.append(f"MemTotal:       {total} kB")
        lines.append("MemFree:        10 kB")
        if available is not None:
            lines.append(f"MemAvailable:   {available} kB")
        lines.append("HugePages_Total:       0")
        self._put(self.proc / "meminfo", "\n".join(lines) + "\n" + extra)

    def write_netdev(self, entries: dict[str, tuple[int, int]], glue: str = " ") -> None:
        head = (
            "Inter-|   Receive                            |  Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n"
        )
        rows = []
        for name, (rx, tx) in entries.items():
            rows.append(f"{name:>6}:{glue}{rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0")
        self._put(self.proc / "net" / "dev", head + "\n".join(rows) + "\n")

    def write_route(self, rows: list[tuple[str, str, int, str]]) -> None:
        """rows are (iface, destination, metric, mask)."""
        text = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        )
        for iface, dest, metric, mask in rows:
            text += f"{iface}\t{dest}\t0101A8C0\t0003\t0\t0\t{metric}\t{mask}\t0\t0\t0\n"
        self._put(self.proc / "net" / "route", text)

    def add_hwmon(self, index: int, name: str, labels_and_temps: list[tuple[str, int]]) -> None:
        base = self.sys / "class" / "hwmon" / f"hwmon{index}"
        self._put(base / "name", name + "\n")
        for n, (label, milli) in enumerate(labels_and_temps, start=1):
            self._put(base / f"temp{n}_label", label + "\n")
            self._put(base / f"temp{n}_input", f"{milli}\n")

    def add_cpu(self, n: int, khz: int) -> None:
        path = self.sys / "devices" / "system" / "cpu" / f"cpu{n}" / "cpufreq"
        self._put(path / "scaling_cur_freq", f"{khz}\n")

    def populate(self) -> None:
        """A complete, healthy host."""
        self.write_stat(user=100, system=50, idle=800, iowait=50)
        self.write_meminfo(total=1000, available=250)
        self.write_netdev({"lo": (5, 5), "wlan0": (1000, 500)})
        self.write_route([("wlan0", "00000000", 600, "00000000")])
        self.add_hwmon(3, "k10temp", [("Tctl", 34500)])
        self.add_cpu(0, 3000000)
        self.add_cpu(1, 4000000)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        handler = logging.NullHandler()  # keep expected warnings off stderr
        logging.getLogger(LOGGER).addHandler(handler)
        self.addCleanup(logging.getLogger(LOGGER).removeHandler, handler)
        self.tree = Tree(self._tmp.name)
        self.clock = FakeClock()

    def reader(self, **kw) -> PerfReader:
        kw.setdefault("clock", self.clock)
        return PerfReader(proc_root=str(self.tree.proc), sys_root=str(self.tree.sys), **kw)

    def baseline(self, **kw) -> PerfReader:
        """A populated tree whose first (baseline) call has already happened."""
        self.tree.populate()
        r = self.reader(**kw)
        self.assertIsNone(r())
        self.clock.advance(2.0)
        return r


class FirstCallTest(Base):
    def test_first_call_none_second_gives_numbers(self):
        r = self.baseline()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"lo": (5, 5), "wlan0": (1000 + 20480, 500 + 4096)})
        result = r()
        self.assertIsNotNone(result)
        self.assertEqual(len(result), 6)

    def test_tuple_order_and_int_types(self):
        r = self.baseline()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"lo": (5, 5), "wlan0": (1000 + 20480, 500 + 4096)})
        mem, cpu, freq, temp, up, down = r()
        self.assertEqual((mem, cpu, freq, temp, up, down), (75, 25, 3500, 35, 2, 10))
        for value in (mem, cpu, freq, temp, up, down):
            self.assertIs(type(value), int)


class CpuTest(Base):
    def second(self, **stat):
        r = self.baseline()
        self.tree.write_stat(**stat)
        return r()

    def test_known_delta_exact_percent(self):
        self.assertEqual(self.second(user=300, system=100, idle=1500, iowait=100)[1], 25)

    def test_iowait_counts_as_idle(self):
        # Only iowait moved: the CPU was not busy.
        self.assertEqual(self.second(user=100, system=50, idle=800, iowait=1050)[1], 0)

    def test_idle_only_is_zero_busy_and_all_busy_is_100(self):
        self.assertEqual(self.second(user=1100, system=50, idle=800, iowait=50)[1], 100)

    def test_guest_fields_are_ignored(self):
        got = self.second(user=300, system=100, idle=1500, iowait=100, guest=9999, guest_nice=9999)
        self.assertEqual(got[1], 25)

    def test_irq_softirq_steal_count_as_busy(self):
        # delta: irq 100, softirq 100, steal 100, idle 700 => 300/1000 busy... total 1000.
        got = self.second(user=100, system=50, idle=1500, iowait=50, irq=100, softirq=100,
                          steal=100)  # fmt: skip
        # d_total = 700 + 300 = 1000, d_idle = 700 => 30
        self.assertEqual(got[1], 30)

    def test_zero_delta_gives_none(self):
        self.assertIsNone(self.second(user=100, system=50, idle=800, iowait=50))

    def test_counter_going_backwards_gives_none_for_that_call(self):
        self.assertIsNone(self.second(user=10, system=5, idle=80, iowait=5))

    def test_old_sample_kept_after_zero_delta(self):
        r = self.baseline()
        self.assertIsNone(r())  # unchanged stat: d_total == 0
        self.clock.advance(2.0)
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"lo": (5, 5), "wlan0": (1000, 500)})
        self.assertEqual(r()[1], 25)  # delta is against the ORIGINAL baseline


class MemoryTest(Base):
    def mem(self, **kw):
        r = self.baseline()
        self.tree.write_meminfo(**kw)
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        return r()

    def test_normal(self):
        self.assertEqual(self.mem(total=1000, available=250)[0], 75)

    def test_rounds(self):
        self.assertEqual(self.mem(total=3, available=1)[0], 67)

    def test_clamped_when_available_exceeds_total(self):
        self.assertEqual(self.mem(total=1000, available=2000)[0], 0)

    def test_missing_mem_available_gives_none(self):
        self.assertIsNone(self.mem(total=1000, available=None))

    def test_missing_mem_total_gives_none(self):
        self.assertIsNone(self.mem(total=None, available=10))

    def test_zero_mem_total_gives_none(self):
        self.assertIsNone(self.mem(total=0, available=0))

    def test_garbage_value_gives_none(self):
        self.assertIsNone(self.mem(total=1000, available=250, extra="MemAvailable: x kB\n"))


class FrequencyTest(Base):
    def freq(self, setup):
        r = self.baseline()
        setup()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        return r()

    def test_average_of_all_cores(self):
        got = self.freq(lambda: self.tree.add_cpu(2, 5000000))
        self.assertEqual(got[2], 4000)  # (3000 + 4000 + 5000) / 3

    def test_rounds_half_up_to_whole_mhz(self):
        def setup():
            self.tree.add_cpu(0, 3001499)
            self.tree.add_cpu(1, 3000000)
            self.tree.add_cpu(2, 3000000)

        self.assertEqual(self.freq(setup)[2], 3000)  # 3000.499 MHz

        def setup2():
            self.tree.add_cpu(0, 1500)
            self.tree.add_cpu(1, 1500)
            self.tree.add_cpu(2, 1500)

        self.assertEqual(self.freq(setup2)[2], 2)  # 1.5 MHz rounds up

    def test_one_missing_file_is_ignored(self):
        def setup():
            (self.tree.sys / "devices/system/cpu/cpu5/cpufreq").mkdir(parents=True)  # no file

        self.assertEqual(self.freq(setup)[2], 3500)

    def test_one_unparsable_file_is_ignored(self):
        def setup():
            self.tree.add_cpu(5, 0)
            (self.tree.sys / "devices/system/cpu/cpu5/cpufreq/scaling_cur_freq").write_text("x\n")

        self.assertEqual(self.freq(setup)[2], 3500)

    def test_none_readable_gives_none(self):
        def setup():
            for n in (0, 1):
                (self.tree.sys / f"devices/system/cpu/cpu{n}/cpufreq/scaling_cur_freq").unlink()

        self.assertIsNone(self.freq(setup))

    def test_non_cpu_directories_are_not_globbed(self):
        def setup():
            self.tree.add_cpu(2, 1)
            d = self.tree.sys / "devices/system/cpu/cpufreq/policy0"
            d.mkdir(parents=True)
            (d / "scaling_cur_freq").write_text("1\n")

        # cpu2 has 1 kHz => (3000000 + 4000000 + 1) / 3 kHz = 2333 MHz
        self.assertEqual(self.freq(setup)[2], 2333)


class TemperatureTest(Base):
    def temp(self, setup):
        r = self.baseline()
        setup()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        return r()

    def test_k10temp_found_even_as_hwmon7(self):
        def setup():
            hw = self.tree.sys / "class/hwmon"
            os.rename(hw / "hwmon3", hw / "hwmon7")

        self.assertEqual(self.temp(setup)[3], 35)

    def test_other_device_names_are_ignored(self):
        def setup():
            self.tree.add_hwmon(0, "nvme", [("Tctl", 99000)])
            self.tree.add_hwmon(1, "acpitz", [("Tctl", 98000)])

        self.assertEqual(self.temp(setup)[3], 35)

    def test_no_tctl_label_gives_none_without_fallback(self):
        def setup():
            self.tree.add_hwmon(3, "k10temp", [("Tccd1", 50000), ("Tdie", 51000)])

        self.assertIsNone(self.temp(setup))

    def test_label_picks_the_matching_input_not_temp1(self):
        def setup():
            self.tree.add_hwmon(3, "k10temp", [("Tccd1", 50000), ("Tctl", 61000)])

        self.assertEqual(self.temp(setup)[3], 61)

    def test_no_k10temp_gives_none(self):
        def setup():
            os.rename(self.tree.sys / "class/hwmon/hwmon3/name", self.tree.sys / "x")
            (self.tree.sys / "class/hwmon/hwmon3/name").write_text("coretemp\n")

        self.assertIsNone(self.temp(setup))

    def test_rounding_is_half_up_to_whole_degrees(self):
        # PINNED: (millideg + 500) // 1000, so 34499 -> 34 and 34500 -> 35.
        for milli, want in ((34499, 34), (34500, 35), (34999, 35), (35000, 35), (0, 0)):
            with self.subTest(milli=milli):
                self.setUp()
                got = self.temp(lambda m=milli: self.tree.add_hwmon(3, "k10temp", [("Tctl", m)]))
                self.assertEqual(got[3], want)

    def test_rediscovers_when_the_cached_path_stops_working(self):
        r = self.baseline()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.assertEqual(r()[3], 35)
        self.clock.advance(2.0)
        hw = self.tree.sys / "class/hwmon"
        os.rename(hw / "hwmon3", hw / "hwmon9")  # index changed (e.g. after a re-probe)
        (hw / "hwmon9/temp1_input").write_text("40000\n")
        self.tree.write_stat(user=600, system=200, idle=2200, iowait=150)
        self.assertEqual(r()[3], 40)


class NetworkTest(Base):
    def net(self, entries, *, route=None, interfaces=(), seconds=2.0, base=None):
        self.tree.populate()
        if base is not None:
            self.tree.write_netdev(base)
        r = self.reader(interfaces=interfaces)
        r()
        self.clock.advance(seconds)
        if route is not None:
            self.tree.write_route(route)
        self.tree.write_netdev(entries)
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        return r()

    def test_rate_over_two_second_interval(self):
        got = self.net({"lo": (5, 5), "wlan0": (1000 + 20480, 500 + 4096)})
        self.assertEqual((got[4], got[5]), (2, 10))  # tx 4096/1024/2 = 2, rx 20480/1024/2 = 10

    def test_rate_scales_with_elapsed_time(self):
        got = self.net({"lo": (5, 5), "wlan0": (1000 + 20480, 500 + 4096)}, seconds=4.0)
        self.assertEqual((got[4], got[5]), (1, 5))

    def test_kb_per_second_is_truncated(self):
        # rx 3000 B over 2 s = 1.46 KB/s -> 1 ; tx 4095 B = 1.9995 KB/s -> 1
        got = self.net({"wlan0": (1000 + 3000, 500 + 4095)})
        self.assertEqual((got[4], got[5]), (1, 1))

    def test_counter_going_backwards_counts_as_zero(self):
        got = self.net({"wlan0": (10, 20480 + 500)})
        self.assertEqual((got[4], got[5]), (10, 0))

    def test_default_route_picks_lowest_metric(self):
        route = [
            ("eth0", "00000000", 700, "00000000"),
            ("wlan0", "00000000", 600, "00000000"),
            ("wlan0", "0001A8C0", 1, "00FFFFFF"),  # not a default route, lower metric
        ]
        # Make eth0 unknown so that picking it would give None.
        got = self.net({"wlan0": (1000 + 20480, 500)}, route=route)
        self.assertEqual(got[5], 10)

    def test_default_route_other_order(self):
        route = [
            ("wlan0", "00000000", 700, "00000000"),
            ("eth0", "00000000", 100, "00000000"),
        ]
        got = self.net(
            {"wlan0": (1000, 500), "eth0": (2048 * 2, 0)},
            route=route,
            base={"wlan0": (1000, 500), "eth0": (0, 0)},
        )
        self.assertEqual(got, (75, 25, 3500, 35, 0, 2))

    def test_route_with_no_default_gives_none(self):
        got = self.net({"wlan0": (99999, 99999)}, route=[("wlan0", "0001A8C0", 600, "00FFFFFF")])
        self.assertIsNone(got)

    def test_default_destination_with_non_zero_mask_is_not_default(self):
        got = self.net({"wlan0": (99999, 99999)}, route=[("wlan0", "00000000", 1, "00FFFFFF")])
        self.assertIsNone(got)

    def test_configured_interface_overrides_the_route(self):
        got = self.net(
            {"wlan0": (99999, 99999), "eth0": (2048 * 2, 4096)},
            interfaces=("eth0",),
            base={"wlan0": (1000, 500), "eth0": (0, 0)},
        )
        self.assertEqual((got[4], got[5]), (2, 2))

    def test_configured_interface_works_without_any_route(self):
        self.tree.populate()
        (self.tree.proc / "net" / "route").unlink()
        self.tree.write_netdev({"eth0": (0, 0)})
        r = self.reader(interfaces=("eth0",))
        r()
        self.clock.advance(2.0)
        self.tree.write_netdev({"eth0": (2048 * 2, 0)})
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.assertEqual(r()[5], 2)

    def test_two_interfaces_are_summed(self):
        got = self.net(
            {"wlan0": (1000 + 2048 * 2, 500 + 2048 * 2), "eth0": (2048 * 4, 2048 * 6)},
            interfaces=("wlan0", "eth0"),
            base={"wlan0": (1000, 500), "eth0": (0, 0)},
        )
        self.assertEqual((got[4], got[5]), (2 + 6, 2 + 4))  # tx 2+6, rx 2+4 (KB/s)

    def test_summing_ignores_a_backwards_counter_per_interface(self):
        got = self.net(
            {"wlan0": (0, 500), "eth0": (2048 * 4, 0)},
            interfaces=("wlan0", "eth0"),
            base={"wlan0": (1000, 500), "eth0": (0, 0)},
        )
        self.assertEqual(got[5], 4)  # wlan0 went backwards: 0, not -0.5

    def test_unknown_interface_gives_none(self):
        got = self.net({"wlan0": (1, 1)}, interfaces=("nope0",), base={"wlan0": (0, 0)})
        self.assertIsNone(got)

    def test_one_of_two_interfaces_unknown_gives_none(self):
        got = self.net({"wlan0": (1, 1)}, interfaces=("wlan0", "nope0"), base={"wlan0": (0, 0)})
        self.assertIsNone(got)

    def test_zero_elapsed_gives_none(self):
        got = self.net({"wlan0": (99999, 99999)}, seconds=0.0)
        self.assertIsNone(got)

    def test_huge_rate_is_clamped_to_int32(self):
        r = self.baseline()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"wlan0": (1000 + 2**62, 500)})
        self.assertEqual(r()[5], INT32_MAX)


class ParserTest(Base):
    def test_netdev_with_no_space_after_the_colon(self):
        self.tree.populate()
        self.tree.write_netdev({"wlan0": (1234567890123456, 500)}, glue="")
        r = self.reader()
        r()
        self.clock.advance(2.0)
        self.tree.write_netdev({"wlan0": (1234567890123456 + 2048 * 2, 500)}, glue="")
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.assertEqual(r()[5], 2)

    def test_netdev_garbage_line_gives_none(self):
        r = self.baseline()
        (self.tree.proc / "net" / "dev").write_text("h1\nh2\n wlan0: abc 1 0 0 0 0 0 0 5\n")
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.assertIsNone(r())

    def test_stat_garbage_gives_none(self):
        r = self.baseline()
        (self.tree.proc / "stat").write_text("cpu  a b c\n")
        self.assertIsNone(r())

    def test_stat_with_too_few_fields_gives_none(self):
        r = self.baseline()
        (self.tree.proc / "stat").write_text("cpu  1 2 3\n")
        self.assertIsNone(r())

    def test_default_route_interface_helper(self):
        self.tree.write_route(
            [("a0", "00000000", 50, "00000000"), ("b0", "00000000", 20, "00000000")]
        )
        self.assertEqual(default_route_interface(str(self.tree.proc)), "b0")

    def test_default_route_interface_helper_none_cases(self):
        self.assertIsNone(default_route_interface(str(self.tree.proc)))  # no file
        self.tree.write_route([("a0", "0001A8C0", 50, "00FFFFFF")])
        self.assertIsNone(default_route_interface(str(self.tree.proc)))
        (self.tree.proc / "net" / "route").write_text("Iface\n\xff\x00junk\n")
        self.assertIsNone(default_route_interface(str(self.tree.proc)))


class CacheTest(Base):
    def ready(self):
        r = self.baseline()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"lo": (5, 5), "wlan0": (1000 + 20480, 500 + 4096)})
        first = r()
        self.assertIsNotNone(first)
        return r, first

    def test_second_call_inside_cache_s_reads_nothing(self):
        r, first = self.ready()
        for path in (self.tree.proc, self.tree.sys):
            for child in sorted(path.rglob("*"), reverse=True):
                child.unlink() if child.is_file() else child.rmdir()
        self.clock.advance(1.9)
        self.assertEqual(r(), first)

    def test_cache_expires_at_cache_s(self):
        r, first = self.ready()
        self.clock.advance(2.0)
        self.tree.write_meminfo(total=1000, available=500)
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)  # d_total == 0
        self.assertIsNone(r())  # re-read happened (and the CPU delta was zero)

    def test_none_is_not_cached(self):
        r = self.baseline()
        (self.tree.proc / "meminfo").unlink()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.assertIsNone(r())
        self.tree.write_meminfo(total=1000, available=500)
        self.tree.write_netdev({"lo": (5, 5), "wlan0": (1000, 500)})
        self.tree.write_stat(user=600, system=200, idle=2200, iowait=150)
        self.clock.advance(0.1)  # inside cache_s, but a None must not have been cached
        got = r()
        self.assertIsNotNone(got)
        self.assertEqual(got[0], 50)

    def test_custom_cache_s(self):
        r = self.baseline(cache_s=0.0)
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"wlan0": (1000 + 20480, 500)})
        self.assertIsNotNone(r())
        self.tree.write_meminfo(total=1000, available=0)
        self.clock.advance(0.5)
        self.tree.write_stat(user=500, system=100, idle=2500, iowait=100)
        self.assertEqual(r()[0], 100)  # same clock time, re-read because cache_s == 0


class FailureLoggingTest(Base):
    def test_warning_once_per_outage_and_info_on_recovery(self):
        self.tree.populate()
        hw = self.tree.sys / "class/hwmon/hwmon3/name"
        hw.write_text("other\n")
        r = self.reader()
        with self.assertLogs(LOGGER, level="INFO") as cm:
            for _ in range(3):
                r()
                self.clock.advance(2.0)
            hw.write_text("k10temp\n")
            self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
            r()
        levels = [rec.levelname for rec in cm.records]
        self.assertEqual(levels.count("WARNING"), 1, cm.output)
        self.assertEqual(levels.count("INFO"), 1, cm.output)
        self.assertLess(levels.index("WARNING"), levels.index("INFO"))
        self.assertIn("temperature", cm.output[0])

    def test_a_second_outage_warns_again(self):
        self.tree.populate()
        name = self.tree.sys / "class/hwmon/hwmon3/name"
        r = self.reader()
        with self.assertLogs(LOGGER, level="INFO") as cm:
            name.write_text("other\n")
            r()
            self.clock.advance(2.0)
            name.write_text("k10temp\n")
            r()
            self.clock.advance(2.0)
            name.write_text("other\n")
            r()
        self.assertEqual([x.levelname for x in cm.records].count("WARNING"), 2)

    def test_normal_baseline_and_zero_delta_do_not_warn(self):
        r = self.baseline()
        with self.assertNoLogs(LOGGER, level="WARNING"):
            r()  # zero CPU delta
            self.clock.advance(2.0)
            r()


class NeverRaisesTest(Base):
    def test_empty_tree_gives_none_and_does_not_raise(self):
        r = self.reader()
        self.assertIsNone(r())
        self.clock.advance(2.0)
        self.assertIsNone(r())

    def test_oserror_on_every_file_gives_none(self):
        self.tree.populate()
        r = self.reader()
        with (
            mock.patch.object(Path, "read_text", side_effect=OSError("boom")),
            mock.patch.object(Path, "glob", side_effect=OSError("boom")),
            mock.patch.object(Path, "iterdir", side_effect=OSError("boom")),
        ):
            self.assertIsNone(r())
            self.clock.advance(2.0)
            self.assertIsNone(r())

    def test_unicode_garbage_in_files_gives_none(self):
        self.tree.populate()
        (self.tree.proc / "meminfo").write_bytes(b"\xff\xfe\x00junk")
        r = self.reader()
        r()
        self.clock.advance(2.0)
        self.assertIsNone(r())

    def test_state_lives_on_the_instance(self):
        a = self.baseline()
        self.tree.write_stat(user=300, system=100, idle=1500, iowait=100)
        self.tree.write_netdev({"wlan0": (1000 + 20480, 500)})
        b = self.reader()
        self.assertIsNone(b())  # b has no baseline even though a has one
        self.assertIsNotNone(a())

    def test_import_does_not_touch_the_real_proc(self):
        with (
            mock.patch.object(Path, "read_text", side_effect=AssertionError("read at import")),
            mock.patch("builtins.open", side_effect=AssertionError("open at import")),
        ):
            importlib.reload(perf_reader)
        importlib.reload(perf_reader)


@unittest.skipUnless(os.path.exists("/proc/stat"), "no /proc/stat")
class RealHostSmokeTest(unittest.TestCase):
    def test_types_and_ranges(self):
        import time

        r = PerfReader(cache_s=0.0)
        r()
        time.sleep(0.2)
        got = r()
        if got is None:
            self.skipTest("a source is unavailable on this host (e.g. no default route)")
        mem, cpu, freq, temp, up, down = got
        for value in got:
            self.assertIs(type(value), int)
        self.assertTrue(0 <= mem <= 100)
        self.assertTrue(0 <= cpu <= 100)
        self.assertGreater(freq, 0)
        self.assertTrue(0 <= temp <= 150)
        self.assertGreaterEqual(up, 0)
        self.assertGreaterEqual(down, 0)


if __name__ == "__main__":
    unittest.main()
