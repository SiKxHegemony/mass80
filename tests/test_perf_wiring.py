"""T28: the performance reader wired into the daemon: frame limit, provider, banner, 12a replay."""

import dataclasses
import io
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from mass80d import daemon
from mass80d.config import default_config, load_config
from mass80d.daemon import EXIT_OK, banner, build_dispatcher, run_replay
from mass80d.parser import KIND_POLL, Event
from mass80d.replies import ReplyLayer, build_get_performance

REPO = Path(__file__).resolve().parent.parent
STAGE12A = str(REPO / "configs" / "stage12a-performance.toml")
VALUES = (40, 12, 3400, 45, 10, 200)


def poll():
    return Event(KIND_POLL, "get_performance")


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class Provider:
    def __init__(self, values=VALUES):
        self.values = values
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.values


def cfg_on(max_replies=0):
    return dataclasses.replace(
        default_config(), reply_get_performance=True, perf_max_replies=max_replies
    )


class FrameLimitTest(unittest.TestCase):
    def make(self, cfg, provider):
        self.written = []
        self.clock = Clock()
        return ReplyLayer(self.written.append, cfg, performance_provider=provider, clock=self.clock)

    def step(self, layer):
        self.clock.t += 3.0
        return layer.on_poll(poll())

    def test_poll_sends_exactly_the_built_frame(self):
        r = self.make(cfg_on(), Provider())
        self.assertTrue(self.step(r))
        self.assertEqual(self.written, [build_get_performance(*VALUES)])

    def test_limit_two_sends_two_then_stops_without_calling_the_provider(self):
        p = Provider()
        r = self.make(cfg_on(2), p)
        self.assertTrue(self.step(r))
        self.assertTrue(self.step(r))
        self.assertEqual(p.calls, 2)
        with self.assertLogs("mass80d.replies", "INFO") as cm:
            self.assertFalse(self.step(r))
            self.assertFalse(self.step(r))
            self.assertFalse(self.step(r))
        self.assertEqual(p.calls, 2)  # NOT called after the limit
        self.assertEqual(len(self.written), 2)
        self.assertEqual(len(cm.records), 1)  # logged once, not every poll
        self.assertIn("performance: frame limit reached, not answering", cm.output[0])

    def test_none_is_not_counted(self):
        p = Provider(None)
        r = self.make(cfg_on(1), p)
        for _ in range(3):
            self.assertFalse(self.step(r))
        self.assertEqual(self.written, [])
        p.values = VALUES
        self.assertTrue(self.step(r))  # still within the limit of 1
        self.assertEqual(len(self.written), 1)
        self.assertEqual(p.calls, 4)

    def test_failed_write_is_not_counted(self):
        p = Provider()
        r = self.make(cfg_on(1), p)
        self.clock.t += 3.0
        r._write = mock.Mock(side_effect=OSError("boom"))
        with self.assertLogs("mass80d.replies", "WARNING"):
            self.assertFalse(r.on_poll(poll()))
        r._write = self.written.append
        self.assertTrue(self.step(r))
        self.assertEqual(len(self.written), 1)

    def test_unlimited_over_ten_polls(self):
        p = Provider()
        r = self.make(cfg_on(0), p)
        for _ in range(10):
            self.assertTrue(self.step(r))
        self.assertEqual(len(self.written), 10)
        self.assertEqual(p.calls, 10)

    def test_flag_off_sends_nothing_and_never_calls_the_provider(self):
        p = Provider()
        r = self.make(default_config(), p)
        for _ in range(3):
            self.assertFalse(self.step(r))
        self.assertEqual(self.written, [])
        self.assertEqual(p.calls, 0)


class FakeReader:
    instances: list = []

    def __init__(self, **kw):
        self.kw = kw
        FakeReader.instances.append(self)

    def __call__(self):
        return VALUES


def _build(config, **kw):
    kw.setdefault("list_players", lambda: daemon.NO_PLAYER)
    return build_dispatcher(config, lambda b: None, lambda argv: True, **kw)


class BuildDispatcherTest(unittest.TestCase):
    def setUp(self):
        FakeReader.instances = []

    def test_flag_on_live_builds_a_reader_with_the_interfaces(self):
        cfg = dataclasses.replace(cfg_on(), perf_interfaces=("eth0", "wlan0"))
        with mock.patch.object(daemon, "PerfReader", FakeReader):
            _build(cfg, start_services=True)
        self.assertEqual(len(FakeReader.instances), 1)
        self.assertEqual(FakeReader.instances[0].kw, {"interfaces": ("eth0", "wlan0")})

    def test_flag_off_builds_none(self):
        with mock.patch.object(daemon, "PerfReader", FakeReader):
            _build(default_config(), start_services=True)
        self.assertEqual(FakeReader.instances, [])

    def test_replay_mode_builds_none_and_never_touches_proc(self):
        boom = mock.Mock(side_effect=AssertionError("PerfReader constructed"))
        with mock.patch.object(daemon, "PerfReader", boom):
            _build(cfg_on(), start_services=False)
        boom.assert_not_called()

    def test_injected_provider_wins_and_no_reader_is_built(self):
        p = Provider()
        boom = mock.Mock(side_effect=AssertionError("PerfReader constructed"))
        for now_func in (None, lambda: daemon.datetime.now().astimezone()):
            with (
                mock.patch.object(daemon, "PerfReader", boom),
                mock.patch.object(daemon, "ReplyLayer") as layer,
            ):
                _build(cfg_on(), now_func=now_func, performance_provider=p)
            self.assertIs(layer.call_args.kwargs["performance_provider"], p)
        boom.assert_not_called()

    def test_built_reader_reaches_the_reply_layer_in_both_branches(self):
        for now_func in (None, lambda: daemon.datetime.now().astimezone()):
            FakeReader.instances = []
            with (
                mock.patch.object(daemon, "PerfReader", FakeReader),
                mock.patch.object(daemon, "ReplyLayer") as layer,
            ):
                _build(cfg_on(), now_func=now_func, start_services=True)
            self.assertIs(layer.call_args.kwargs["performance_provider"], FakeReader.instances[0])

    def test_no_provider_when_flag_off(self):
        with mock.patch.object(daemon, "ReplyLayer") as layer:
            _build(default_config(), start_services=True)
        self.assertIsNone(layer.call_args.kwargs["performance_provider"])


class BannerTest(unittest.TestCase):
    def lines(self, **kw):
        return banner("x", "p", False, False, 10.0, "live", **kw)

    def test_default_interface_one_frame(self):
        lines = self.lines(performance=True, performance_max_replies=1)
        self.assertEqual(
            lines[-1],
            "  performance: ON, reads /proc and /sys (read-only), interface default route, "
            "1 frame(s) at most",
        )

    def test_named_interfaces(self):
        lines = self.lines(
            performance=True, performance_interfaces=("eth0", "wlan0"), performance_max_replies=5
        )
        self.assertEqual(
            lines[-1],
            "  performance: ON, reads /proc and /sys (read-only), interface eth0, wlan0, "
            "5 frame(s) at most",
        )

    def test_unlimited(self):
        lines = self.lines(performance=True)
        self.assertEqual(
            lines[-1],
            "  performance: ON, reads /proc and /sys (read-only), interface default route, "
            "unlimited",
        )

    def test_banner_unchanged_when_off(self):
        off = self.lines()
        self.assertEqual(
            off,
            [
                "mass80d starting (live)",
                "  config:  x",
                "  port:    p",
                "  WRITES to device:   off (--no-write)",
                "  system ACTIONS:     off (--no-actions)",
                "  silence timeout: 10.0 s",
            ],
        )
        self.assertEqual(self.lines(performance=False, performance_interfaces=("eth0",)), off)


def _rx(t, obj):
    line = json.dumps(obj) + "\n"
    return json.dumps({"kind": "rx", "t": t, "hex": line.encode().hex()})


class Stage12aReplayTest(unittest.TestCase):
    def test_replay_of_the_12a_config_writes_nothing_and_builds_no_reader(self):
        config = load_config(STAGE12A)
        captured = []
        real = daemon.WriteGate

        def factory(*a, **k):
            g = real(*a, **k)
            captured.append(g)
            return g

        boom = mock.Mock(side_effect=AssertionError("PerfReader constructed"))
        before = threading.enumerate()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.log")
            Path(p).write_text(
                "\n".join(_rx(float(i) * 3.0, {"action": "get_performance"}) for i in range(4))
                + "\n"
            )
            with (
                mock.patch.object(daemon, "PerfReader", boom),
                mock.patch.object(daemon, "WriteGate", side_effect=factory),
                self.assertNoLogs("mass80d", "WARNING"),
            ):
                code = run_replay(config, p, out)
        self.assertEqual(code, EXIT_OK)
        boom.assert_not_called()
        self.assertEqual(threading.enumerate(), before)
        self.assertEqual(captured[0].suppressed, [])  # no get_performance frame
        self.assertIn("get_performance: 4", out.getvalue())
        self.assertIn("would-write frames: 0", out.getvalue())


class PrintConfigTest(unittest.TestCase):
    def test_print_config_for_the_12a_config(self):
        out, err = io.StringIO(), io.StringIO()
        code = daemon.main(
            ["--config", STAGE12A, "--print-config"], out=out, err=err, install_signals=False
        )
        self.assertEqual(code, EXIT_OK)
        cfg = json.loads(out.getvalue())["config"]
        self.assertIs(cfg["reply_get_performance"], True)
        self.assertEqual(cfg["perf_max_replies"], 1)
        self.assertEqual(cfg["perf_interfaces"], [])


if __name__ == "__main__":
    unittest.main()
