import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from mass80d.actions import Actions, CommandRunner
from mass80d.config import default_config, load_config_text
from mass80d.dispatcher import Dispatcher, pump
from mass80d.parser import KIND_INPUT, KIND_POLL, KIND_UNKNOWN, Event, EventParser
from mass80d.replies import ReplyLayer
from mass80d.volume_reader import VolumeReading

EDT = timezone(timedelta(hours=-4))


class FakeRun:
    def __init__(self):
        self.argvs = []

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def build(cfg=None):
    cfg = cfg or default_config()
    run, clock, written = FakeRun(), Clock(), []
    actions = Actions(cfg, CommandRunner(run), clock)
    replies = ReplyLayer(
        written.append,
        cfg,
        clock=clock,
        now_func=lambda: datetime(2026, 10, 9, 0, 42, 17, tzinfo=EDT),
    )
    return Dispatcher(actions, replies), run, written, clock


def ev(action, **kw):
    return Event(KIND_INPUT, action, **kw)


class DispatcherTest(unittest.TestCase):
    def test_volume_value_runs_wpctl_and_begin_marker_does_not(self):
        d, run, _, _ = build()
        d.handle(ev("volume"))  # begin marker
        self.assertEqual(run.argvs, [])
        d.handle(ev("volume", value=100))
        self.assertEqual(run.argvs, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.5"]])

    def test_music(self):
        d, run, _, _ = build()
        d.handle(ev("music_play"))
        d.handle(ev("music_pause"))
        self.assertEqual(run.argvs, [["playerctl", "play"], ["playerctl", "pause"]])

    def test_all_four_music_actions_reach_actions_music(self):
        d, _, _, _ = build()
        with mock.patch.object(d._actions, "music") as m:
            for a in ("music_play", "music_pause", "music_prev", "music_next"):
                d.handle(ev(a))
        self.assertEqual(
            [c.args[0] for c in m.call_args_list],
            ["music_play", "music_pause", "music_prev", "music_next"],
        )

    def test_unknown_music_like_actions_do_not_reach_music(self):
        d, run, _, _ = build()
        with mock.patch.object(d._actions, "music") as m:
            for a in ("music_seek", "music_stop", "music_", "get_cover_next"):
                d.handle(ev(a))
        m.assert_not_called()
        self.assertEqual(run.argvs, [])

    def test_prev_next_run_playerctl_and_leave_volume_and_keys_alone(self):
        d, run, _, _ = build()
        d.handle(ev("music_prev"))
        d.handle(ev("music_next"))
        d.handle(ev("send_key", key="Home"))
        d.handle(ev("volume", value=100))
        self.assertEqual(
            run.argvs,
            [
                ["playerctl", "previous"],
                ["playerctl", "next"],
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.5"],
            ],
        )

    def test_send_key_default_is_harmless(self):
        d, run, written, _ = build()
        d.handle(ev("send_key", key="Page Up"))
        self.assertEqual(run.argvs, [])
        self.assertEqual(written, [])

    def test_send_key_bound(self):
        d, run, _, _ = build(
            load_config_text('[keys."Page Down"]\ntype="hyprctl"\nargs=["workspace","e+1"]\n')
        )
        d.handle(ev("send_key", key="Page Down"))
        self.assertEqual(run.argvs, [["hyprctl", "dispatch", "workspace", "e+1"]])

    def test_light_is_noop(self):
        d, run, written, _ = build()
        d.handle(ev("light"))
        d.handle(ev("light", value=33))
        self.assertEqual((run.argvs, written), ([], []))

    def test_unknown_event_ignored(self):
        d, run, written, _ = build()
        d.handle(Event(KIND_UNKNOWN, "enter_udisk_mode"))
        d.handle(Event(KIND_UNKNOWN, None))
        self.assertEqual((run.argvs, written), ([], []))

    def test_poll_get_time_replied_others_silent_by_default(self):
        d, run, written, _ = build()
        d.handle(Event(KIND_POLL, "get_time", version="2.1.5"))
        d.handle(Event(KIND_POLL, "get_music_info"))
        d.handle(Event(KIND_POLL, "get_performance"))
        self.assertEqual(len(written), 1)
        self.assertIn(b'"action":"get_time"', written[0])
        self.assertEqual(run.argvs, [])

    def test_tick_flushes_coalesced_volume(self):
        d, run, _, clock = build(load_config_text("[volume]\nmax_fraction = 1.0\n"))
        d.handle(ev("volume", value=10))
        clock.t += 0.01
        d.handle(ev("volume", value=40))
        self.assertEqual(len(run.argvs), 1)
        clock.t += 0.5
        d.tick()
        self.assertEqual(len(run.argvs), 2)
        self.assertEqual(run.argvs[-1][-1], "0.4")

    def test_handler_exception_does_not_propagate(self):
        d, _, _, _ = build()

        def boom(*a, **k):
            raise RuntimeError("x")

        d._actions.volume = boom
        d.handle(ev("volume", value=5))  # must not raise


class FakeTransport:
    def __init__(self, chunks):
        self.parser = EventParser()
        self.chunks = list(chunks)
        self.stop = False

    def read_events(self):
        if not self.chunks:
            self.stop = True
            return []
        return self.parser.feed(self.chunks.pop(0))


class VolumeQueryRoutingTest(unittest.TestCase):
    def build(self, enabled=True):
        flag = "true" if enabled else "false"
        cfg = load_config_text(f"[replies]\nvolume_query = {flag}\n[volume]\nmax_fraction = 0.3\n")
        run, clock, written, reads = FakeRun(), Clock(), [], []

        def reader():
            reads.append(1)
            return VolumeReading(0.30, False)

        actions = Actions(cfg, CommandRunner(run), clock)
        replies = ReplyLayer(written.append, cfg, clock=clock, volume_reader=reader)
        return Dispatcher(actions, replies), run, written, reads, clock

    def test_bare_marker_replies_and_runs_no_set_volume(self):
        d, run, written, _, _ = self.build()
        d.handle(ev("volume"))
        self.assertEqual(written, [b'{"query":"volume","result":"ok","value":100}\n'])
        self.assertEqual(run.argvs, [])

    def test_valued_event_still_sets_volume_and_does_not_reply(self):
        d, run, written, reads, _ = self.build()
        d.handle(ev("volume", value=100))
        self.assertEqual(run.argvs, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.3"]])
        self.assertEqual((written, reads), ([], []))

    def test_disabled_writes_nothing_and_never_reads(self):
        d, run, written, reads, _ = self.build(enabled=False)
        d.handle(ev("volume"))
        self.assertEqual((written, reads, run.argvs), ([], [], []))

    def test_other_actions_never_trigger_the_query(self):
        d, _, written, reads, _ = self.build()
        for a in ("light", "music_play"):
            d.handle(ev(a))
        d.handle(ev("send_key", key="Home"))
        self.assertEqual((written, reads), ([], []))


class PumpTest(unittest.TestCase):
    def test_end_to_end_from_raw_bytes(self):
        d, run, written, clock = build()
        raw = (
            b'[CDC-TX] ("action":"volume")\n\r\n{"action":"volume"}\n'
            b'[CDC-TX] ("action":"volume","value":27)\n\r\n{"action":"volume","value":27}\n'
            b'[CDC-TX] ("action":"music_play")\n\r\n{"action":"music_play"}\n'
            b'{"action":"get_time","version":"2.1.5"}\r\n'
        )
        t = FakeTransport([raw[:40], raw[40:]])  # split mid-line
        pump(t, d, lambda: t.stop)
        self.assertEqual(
            run.argvs,
            [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.14"], ["playerctl", "play"]],
        )
        self.assertEqual(len(written), 1)
        self.assertIn(b'"caps":false', written[0])

    def test_pump_survives_deeply_nested_line(self):
        d, run, _, _ = build()
        t = FakeTransport([b'{"a":' + b"[" * 100000 + b'\n{"action":"music_play"}\n'])
        pump(t, d, lambda: t.stop)
        self.assertEqual(run.argvs, [["playerctl", "play"]])


if __name__ == "__main__":
    unittest.main()


class UnknownEventLoggingTest(unittest.TestCase):
    """Unknown device events are still ignored, but their raw payload is logged at DEBUG."""

    def test_unknown_event_logs_raw_payload_at_debug(self):
        d, run, written, _ = build()
        raw = {"action": "open_url", "width": 200, "height": 200, "index": 0}
        with self.assertLogs("mass80d.dispatcher", level="DEBUG") as cm:
            d.handle(Event(KIND_UNKNOWN, "open_url", raw=raw))
        out = "\n".join(cm.output)
        self.assertIn("ignoring unknown event 'open_url'", out)
        for piece in ('"width": 200', '"height": 200', '"index": 0'):
            self.assertIn(piece, out)
        self.assertEqual(run.argvs, [])
        self.assertEqual(written, [])

    def test_long_payload_is_truncated(self):
        d, _, _, _ = build()
        raw = {"action": "open_url", "image_data": "A" * 5000}
        with self.assertLogs("mass80d.dispatcher", level="DEBUG") as cm:
            d.handle(Event(KIND_UNKNOWN, "open_url", raw=raw))
        out = "\n".join(cm.output)
        self.assertLess(len(out), 700)
        self.assertIn("truncated", out)

    def test_unknown_event_without_action_does_not_raise(self):
        d, _, _, _ = build()
        with self.assertLogs("mass80d.dispatcher", level="DEBUG") as cm:
            d.handle(Event(KIND_UNKNOWN, None, raw={"value": 3}))
        self.assertIn("ignoring unknown event None", "\n".join(cm.output))

    def test_unserialisable_payload_does_not_raise(self):
        d, _, _, _ = build()
        with self.assertLogs("mass80d.dispatcher", level="DEBUG"):
            d.handle(Event(KIND_UNKNOWN, "x", raw={"k": object()}))

    def test_nothing_logged_above_debug(self):
        d, _, _, _ = build()
        with self.assertNoLogs("mass80d.dispatcher", level="INFO"):
            d.handle(Event(KIND_UNKNOWN, "get_cover", raw={"width": 1}))


class CoverRequestLoggingTest(unittest.TestCase):
    """Every get_cover* request is logged at DEBUG with its raw payload, whatever happens next."""

    def _handle(self, action, raw, cfg=None):
        d, _, written, _ = build(cfg)
        with self.assertLogs("mass80d.dispatcher", level="DEBUG") as cm:
            d.handle(Event(KIND_UNKNOWN, action, raw=raw))
        return "\n".join(cm.output), written

    def test_get_cover_payload_is_logged_when_replies_are_off(self):
        raw = {"action": "get_cover", "width": 480, "height": 480, "index": 0}
        out, written = self._handle("get_cover", raw)
        self.assertIn("get_cover request", out)
        for piece in ('"width": 480', '"height": 480', '"index": 0'):
            self.assertIn(piece, out)
        self.assertEqual(written, [])

    def test_get_cover_payload_is_logged_when_replies_are_on(self):
        cfg = load_config_text("[replies]\nget_cover = true\n")
        raw = {"action": "get_cover", "width": 480, "height": 480, "index": 2}
        out, _ = self._handle("get_cover", raw, cfg)
        self.assertIn('"index": 2', out)

    def test_prev_and_next_payloads_are_logged(self):
        for action in ("get_cover_prev", "get_cover_next"):
            with self.subTest(action=action):
                raw = {"action": action, "width": 120, "height": 90, "index": 1}
                out, written = self._handle(action, raw)
                self.assertIn(action, out)
                for piece in ('"width": 120', '"height": 90', '"index": 1'):
                    self.assertIn(piece, out)
                self.assertEqual(written, [])

    def test_get_cover_still_reaches_the_reply_layer_and_prev_next_do_not(self):
        d, _, _, _ = build()
        with (
            mock.patch.object(d._replies, "on_cover_request") as m,
            self.assertLogs("mass80d.dispatcher", level="DEBUG"),
        ):
            d.handle(Event(KIND_UNKNOWN, "get_cover", raw={"width": 1}))
            d.handle(Event(KIND_UNKNOWN, "get_cover_prev", raw={"width": 1}))
            d.handle(Event(KIND_UNKNOWN, "get_cover_next", raw={"width": 1}))
        self.assertEqual(m.call_count, 1)

    def test_long_payload_is_truncated_and_odd_payloads_do_not_raise(self):
        out, _ = self._handle("get_cover", {"image_data": "A" * 5000})
        self.assertLess(len(out), 700)
        self.assertIn("truncated", out)
        d, _, _, _ = build()
        with self.assertLogs("mass80d.dispatcher", level="DEBUG"):
            d.handle(Event(KIND_UNKNOWN, "get_cover_next", raw={"k": object()}))

    def test_nothing_above_debug(self):
        d, _, _, _ = build()
        with self.assertNoLogs("mass80d.dispatcher", level="INFO"):
            for a in ("get_cover", "get_cover_prev", "get_cover_next"):
                d.handle(Event(KIND_UNKNOWN, a, raw={"width": 1, "height": 1, "index": 0}))
