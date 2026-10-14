"""T23: cover art wiring (replies, dispatcher, daemon). No device, no network, no threads."""

import base64
import contextlib
import dataclasses
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mass80d import daemon
from mass80d.actions import Actions, CommandRunner
from mass80d.config import ConfigError, default_config, load_config, load_config_text
from mass80d.cover_service import CoverLookup, CoverStatus
from mass80d.daemon import (
    EXIT_BAD_ARGS,
    EXIT_OK,
    StopFlag,
    WriteGate,
    _load_cover_test_image,
    banner,
    build_dispatcher,
    main,
    run,
    run_replay,
)
from mass80d.dispatcher import Dispatcher
from mass80d.parser import KIND_POLL, KIND_UNKNOWN, Event
from mass80d.player_select import PlayerInfo
from mass80d.replies import (
    MAX_COVER_JPEG_BYTES,
    MIN_INTERVAL_S,
    REPLY_ACTIONS,
    ReplyLayer,
    build_get_cover,
)

REPO = Path(__file__).resolve().parent.parent
ASSET = REPO / "configs" / "assets" / "cover-test-240.jpg"
EDT = timezone(timedelta(hours=-4))
JPEG = b"\xff\xd8\xff\xe0tiny-fake-jpeg\xff\xd9"
URL = "https://i.scdn.co/image/SECRETART123"
REQ = {"action": "get_cover", "height": 480, "index": 0, "width": 480}


def cfg(**kw):
    base = "[replies]\nget_cover = true\nreply_get_music_info = true\n"
    c = load_config_text(base)
    return dataclasses.replace(c, **kw) if kw else c


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeService:
    def __init__(self):
        self.table = {}  # url -> CoverLookup
        self.prefetched = []
        self.looked = []
        self.stops = []
        self.prefetch_raises = None
        self.stop_raises = None

    def prefetch(self, url):
        self.prefetched.append(url)
        if self.prefetch_raises:
            raise self.prefetch_raises
        return CoverStatus.PENDING

    def lookup(self, url):
        self.looked.append(url)
        return self.table.get(url, CoverLookup(CoverStatus.UNKNOWN))

    def stop(self, timeout=2.0):
        self.stops.append(timeout)
        if self.stop_raises:
            raise self.stop_raises
        return True


READY = CoverLookup(CoverStatus.READY, JPEG)
PENDING = CoverLookup(CoverStatus.PENDING)
FAILED = CoverLookup(CoverStatus.FAILED)


class FakeSelector:
    def __init__(self, result=None):
        self.result = result

    def select(self):
        return self.result


def info(url=URL, title="T"):
    return PlayerInfo("spotify", "spotify", "Playing", "A", title, 200_000_000, 1_000_000, url)


def req(**raw):
    return Event(KIND_UNKNOWN, "get_cover", raw=dict(REQ, **raw))


def make(service=None, test_jpeg=None, config=None, selector=None):
    clock, written = Clock(), []
    layer = ReplyLayer(
        written.append,
        config or cfg(),
        clock=clock,
        now_func=lambda: datetime(2026, 10, 9, 0, 42, 17, tzinfo=EDT),
        music_selector=selector or FakeSelector(info()),
        cover_service=service,
        cover_test_jpeg=test_jpeg,
    )
    return layer, written, clock


def poll_music(layer, clock, advance=1.0):
    clock.t += advance
    return layer.on_poll(Event(KIND_POLL, "get_music_info"))


class BuildGetCoverTest(unittest.TestCase):
    def test_exact_bytes_key_order_and_newline(self):
        b64 = base64.b64encode(JPEG).decode()
        self.assertEqual(
            build_get_cover(JPEG, 0),
            f'{{"action":"get_cover","image_data":"{b64}","index":0}}\n'.encode(),
        )

    def test_none_gives_the_four_character_null_string(self):
        self.assertEqual(
            build_get_cover(None, 7), b'{"action":"get_cover","image_data":"null","index":7}\n'
        )

    def test_no_newline_inside_and_round_trip(self):
        big = b"\xff\xd8\xff" + bytes(range(256)) * 40
        frame = build_get_cover(big, 3)
        self.assertEqual(frame.count(b"\n"), 1)
        obj = json.loads(frame)
        self.assertEqual(list(obj), ["action", "image_data", "index"])
        self.assertEqual(base64.b64decode(obj["image_data"], validate=True), big)
        self.assertEqual(obj["index"], 3)

    def test_refusals(self):
        for bad in (b"", b"\xff\xd8", b"\x89PNG\r\n", b"\x00\xd8\xff"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_get_cover(bad, 0)
        with self.assertRaises(ValueError):
            build_get_cover(b"\xff\xd8\xff" + b"x" * MAX_COVER_JPEG_BYTES, 0)
        # exactly at the cap is accepted
        build_get_cover(b"\xff\xd8\xff" + b"x" * (MAX_COVER_JPEG_BYTES - 3), 0)
        for idx in (True, False, -1, 1025, 1.0, "0", None):
            with self.assertRaises(ValueError, msg=repr(idx)):
                build_get_cover(JPEG, idx)
        build_get_cover(JPEG, 1024)

    def test_whitelist_and_interval(self):
        self.assertIn("get_cover", REPLY_ACTIONS)
        self.assertEqual(MIN_INTERVAL_S["get_cover"], 0.5)


class CoverRequestTest(unittest.TestCase):
    def sent(self, written):
        return [json.loads(w) for w in written]

    def test_disabled_by_default_writes_nothing_calls_nothing(self):
        svc = FakeService()
        layer, written, _ = make(svc, config=default_config())
        self.assertFalse(layer.on_cover_request(req()))
        layer.tick()
        self.assertEqual(written, [])
        self.assertEqual((svc.looked, svc.prefetched), ([], []))

    def test_ready_on_request_sends_one_frame(self):
        svc = FakeService()
        svc.table[URL] = READY
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        self.assertTrue(layer.on_cover_request(req(index=2)))
        frames = [f for f in self.sent(written) if f["action"] == "get_cover"]
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["index"], 2)
        self.assertEqual(base64.b64decode(frames[0]["image_data"]), JPEG)

    def test_pending_then_ready_via_tick(self):
        svc = FakeService()
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        n = len(written)
        self.assertFalse(layer.on_cover_request(req()))
        svc.table[URL] = PENDING
        clock.t += 1
        layer.tick()
        self.assertEqual(len(written), n)
        svc.table[URL] = READY
        clock.t += 0.1
        layer.tick()
        self.assertEqual(len(written), n + 1)
        self.assertIsNone(layer._cover_pending)
        layer.tick()
        self.assertEqual(len(written), n + 1)

    def test_pending_until_deadline_sends_null_once(self):
        svc = FakeService()
        svc.table[URL] = PENDING
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        n = len(written)
        layer.on_cover_request(req(index=1))
        clock.t += 2.9
        layer.tick()
        self.assertEqual(len(written), n)
        clock.t += 0.2  # 3.1 s >= cover_wait_s 3.0
        layer.tick()
        self.assertEqual(written[n:], [b'{"action":"get_cover","image_data":"null","index":1}\n'])
        layer.tick()
        self.assertEqual(len(written), n + 1)

    def test_deadline_with_reply_null_false_sends_nothing(self):
        svc = FakeService()
        layer, written, clock = make(svc, config=cfg(cover_reply_null=False))
        poll_music(layer, clock)
        n = len(written)
        layer.on_cover_request(req())
        clock.t += 5
        layer.tick()
        self.assertEqual(len(written), n)
        self.assertIsNone(layer._cover_pending)

    def test_failed_sends_null(self):
        svc = FakeService()
        svc.table[URL] = FAILED
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        n = len(written)
        layer.on_cover_request(req())
        self.assertIn(b'"image_data":"null"', written[n])

    def test_failed_while_pending_sends_null_on_tick(self):
        svc = FakeService()
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        n = len(written)
        layer.on_cover_request(req())
        svc.table[URL] = FAILED
        layer.tick()
        self.assertEqual(len(written), n + 1)
        self.assertIn(b'"image_data":"null"', written[-1])

    def test_failed_with_reply_null_false_sends_nothing(self):
        svc = FakeService()
        svc.table[URL] = FAILED
        layer, written, clock = make(svc, config=cfg(cover_reply_null=False))
        poll_music(layer, clock)
        n = len(written)
        layer.on_cover_request(req())
        self.assertEqual(len(written), n)

    def test_empty_art_url_sends_null_at_once(self):
        svc = FakeService()
        layer, written, _ = make(svc, selector=FakeSelector(info(url="")))
        layer.on_poll(Event(KIND_POLL, "get_music_info"))
        n = len(written)
        layer.on_cover_request(req())
        self.assertEqual(written[n:], [b'{"action":"get_cover","image_data":"null","index":0}\n'])
        self.assertEqual(svc.looked, [])

    def test_empty_art_url_with_reply_null_false_is_ignored(self):
        layer, written, _ = make(FakeService(), config=cfg(cover_reply_null=False))
        layer.on_cover_request(req())
        self.assertEqual(written, [])

    def test_missing_service_is_unknown_then_deadline_null(self):
        layer, written, clock = make(None)
        poll_music(layer, clock)
        n = len(written)
        self.assertFalse(layer.on_cover_request(req()))
        clock.t += 3.5
        layer.tick()
        self.assertEqual(len(written), n + 1)

    def test_track_change_while_pending_drops_without_frame(self):
        svc = FakeService()
        sel = FakeSelector(info())
        layer, written, clock = make(svc, selector=sel)
        poll_music(layer, clock)
        layer.on_cover_request(req())
        n = len(written)
        sel.result = info(url="https://i.scdn.co/image/OTHER")
        poll_music(layer, clock)
        n += 1  # the music frame
        svc.table[URL] = READY
        layer.tick()
        self.assertEqual(len(written), n)
        self.assertIsNone(layer._cover_pending)

    def test_newer_request_replaces_older_pending(self):
        svc = FakeService()
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        n = len(written)
        layer.on_cover_request(req(index=1))
        layer.on_cover_request(req(index=9))
        svc.table[URL] = READY
        layer.tick()
        self.assertEqual(len(written), n + 1)
        self.assertEqual(json.loads(written[-1])["index"], 9)

    def test_malformed_requests_write_nothing(self):
        svc = FakeService()
        svc.table[URL] = READY
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        n = len(written)
        bad = [
            {"action": "get_cover", "height": 480, "width": 480},
            {"action": "get_cover", "height": 480, "index": 0},
            {"action": "get_cover", "width": 480, "index": 0},
            dict(REQ, index=0.0),
            dict(REQ, width=480.0),
            dict(REQ, index=True),
            dict(REQ, height=False),
            dict(REQ, index="0"),
            dict(REQ, width="480"),
            dict(REQ, width=0),
            dict(REQ, height=0),
            dict(REQ, width=1025),
            dict(REQ, height=1025),
        ]
        for raw in bad:
            clock.t += 1
            self.assertFalse(layer.on_cover_request(Event(KIND_UNKNOWN, "get_cover", raw=raw)))
        layer.tick()
        self.assertEqual(len(written), n)
        self.assertIsNone(layer._cover_pending)

    def test_index_is_clamped(self):
        svc = FakeService()
        svc.table[URL] = READY
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        layer.on_cover_request(req(index=5000))
        self.assertEqual(json.loads(written[-1])["index"], 1024)
        clock.t += 1
        layer.on_cover_request(req(index=-4))
        self.assertEqual(json.loads(written[-1])["index"], 0)

    def test_rate_limit_keeps_ready_pending_until_sent(self):
        svc = FakeService()
        layer, written, clock = make(svc)
        poll_music(layer, clock)
        svc.table[URL] = READY
        self.assertTrue(layer.on_cover_request(req()))  # sent at clock t
        n = len(written)
        svc.table[URL] = PENDING
        layer.on_cover_request(req(index=4))  # pending (PENDING status)
        svc.table[URL] = READY
        clock.t += 0.1  # inside the 0.5 s interval: rate-limited
        layer.tick()
        self.assertEqual(len(written), n)
        self.assertIsNotNone(layer._cover_pending)
        clock.t += 0.5
        layer.tick()
        self.assertEqual(len(written), n + 1)
        self.assertEqual(json.loads(written[-1])["index"], 4)
        self.assertIsNone(layer._cover_pending)

    def test_failing_write_does_not_raise_from_tick(self):
        from mass80d.transport import TransportError

        svc = FakeService()
        calls = []

        def write(frame):
            calls.append(frame)
            if b"get_cover" in frame:
                raise TransportError("boom")

        clock = Clock()
        layer = ReplyLayer(
            write,
            cfg(),
            clock=clock,
            music_selector=FakeSelector(info()),
            cover_service=svc,
        )
        layer.on_poll(Event(KIND_POLL, "get_music_info"))
        layer.on_cover_request(req())
        svc.table[URL] = READY
        layer.tick()  # must not raise
        svc.lookup = mock.Mock(side_effect=RuntimeError("bug"))
        layer.tick()  # must not raise

    def test_tick_never_raises_when_lookup_raises(self):
        svc = FakeService()
        layer, _, clock = make(svc)
        poll_music(layer, clock)
        layer.on_cover_request(req())
        svc.lookup = mock.Mock(side_effect=RuntimeError("bug"))
        layer.tick()

    def test_tick_without_pending_is_a_noop(self):
        svc = FakeService()
        layer, written, _ = make(svc)
        layer.tick()
        self.assertEqual((written, svc.looked), ([], []))


class PrefetchTest(unittest.TestCase):
    def test_prefetch_called_when_enabled_and_url_present(self):
        svc = FakeService()
        layer, _, clock = make(svc)
        poll_music(layer, clock)
        self.assertEqual(svc.prefetched, [URL])

    def test_not_called_when_disabled(self):
        svc = FakeService()
        layer, _, clock = make(
            svc, config=load_config_text("[replies]\nreply_get_music_info=true\n")
        )
        poll_music(layer, clock)
        self.assertEqual(svc.prefetched, [])

    def test_not_called_without_service_or_with_empty_url(self):
        layer, written, clock = make(None)
        poll_music(layer, clock)
        self.assertEqual(len(written), 1)
        svc = FakeService()
        layer, _, clock = make(svc, selector=FakeSelector(info(url="")))
        poll_music(layer, clock)
        self.assertEqual(svc.prefetched, [])

    def test_prefetch_exception_does_not_stop_the_music_frame(self):
        svc = FakeService()
        svc.prefetch_raises = RuntimeError("bug")
        layer, written, clock = make(svc)
        self.assertTrue(poll_music(layer, clock))
        self.assertEqual(json.loads(written[0])["action"], "get_music_info")

    def test_last_art_url_tracks_and_clears(self):
        from mass80d.music_reader import NO_PLAYER, UNREADABLE, MusicReading

        sel = FakeSelector(info())
        layer, _, clock = make(FakeService(), selector=sel)
        poll_music(layer, clock)
        self.assertEqual(layer._last_art_url, URL)
        for other in (NO_PLAYER, UNREADABLE, MusicReading("Playing", "A", "T", 1, 1)):
            sel.result = info()
            poll_music(layer, clock, advance=3.0)
            self.assertEqual(layer._last_art_url, URL)
            sel.result = other
            poll_music(layer, clock, advance=3.0)
            self.assertEqual(layer._last_art_url, "", repr(other))


class TestModeTest(unittest.TestCase):
    def test_first_request_sends_fixed_picture_second_nothing(self):
        svc = FakeService()
        layer, written, clock = make(svc, test_jpeg=JPEG)
        with self.assertLogs("mass80d.replies", "INFO") as cm:
            self.assertTrue(layer.on_cover_request(req()))
            clock.t += 1
            self.assertFalse(layer.on_cover_request(req()))
            clock.t += 1
            self.assertFalse(layer.on_cover_request(req()))
        self.assertEqual(len(written), 1)
        self.assertEqual(base64.b64decode(json.loads(written[0])["image_data"]), JPEG)
        text = "\n".join(cm.output)
        self.assertIn(f"cover test image sent ({len(JPEG)} bytes)", text)
        self.assertEqual(text.count("reply limit reached"), 1)
        self.assertEqual((svc.looked, svc.prefetched), ([], []))

    def test_zero_means_unlimited(self):
        layer, written, clock = make(config=cfg(cover_test_max_replies=0), test_jpeg=JPEG)
        for _ in range(4):
            clock.t += 1
            self.assertTrue(layer.on_cover_request(req()))
        self.assertEqual(len(written), 4)

    def test_test_mode_works_with_no_service_and_no_player(self):
        layer, written, _ = make(None, test_jpeg=JPEG, selector=FakeSelector(None))
        self.assertTrue(layer.on_cover_request(req()))
        self.assertEqual(len(written), 1)

    def test_rate_limited_send_does_not_use_up_the_limit(self):
        layer, written, clock = make(config=cfg(cover_test_max_replies=2), test_jpeg=JPEG)
        self.assertTrue(layer.on_cover_request(req()))
        self.assertFalse(layer.on_cover_request(req()))  # inside 0.5 s: not written
        clock.t += 1
        self.assertTrue(layer.on_cover_request(req()))
        self.assertEqual(len(written), 2)


class LoggingAndCloseTest(unittest.TestCase):
    def test_url_never_above_debug_and_base64_never_anywhere(self):
        svc = FakeService()
        svc.table[URL] = READY
        layer, written, clock = make(svc)
        b64 = base64.b64encode(JPEG).decode()
        with self.assertLogs("mass80d", "DEBUG") as cm:
            poll_music(layer, clock)
            layer.on_cover_request(req())
            layer.on_cover_request(req(width=0))
            svc.table[URL] = PENDING
            clock.t += 1
            layer.on_cover_request(req())
            clock.t += 4
            layer.tick()
            svc.lookup = mock.Mock(side_effect=RuntimeError(URL))
            layer.on_cover_request(req())
            layer.tick()
            logging.getLogger("mass80d.replies").info("anchor")
        for rec in cm.records:
            text = rec.getMessage()
            self.assertNotIn(b64, text)
            if rec.levelno > logging.DEBUG:
                self.assertNotIn("SECRETART123", text)
                self.assertNotIn("SECRETART123", repr(rec.args))
                self.assertIsNone(rec.exc_info)

    def test_close_stops_once_is_idempotent_and_swallows(self):
        svc = FakeService()
        svc.stop_raises = RuntimeError("x")
        layer, _, _ = make(svc)
        layer.close()
        layer.close()
        self.assertEqual(svc.stops, [2.0])

    def test_close_without_service(self):
        layer, _, _ = make(None)
        layer.close()


class DispatcherCoverTest(unittest.TestCase):
    def setUp(self):
        self.replies = mock.Mock()
        self.actions = mock.Mock()
        self.d = Dispatcher(self.actions, self.replies)

    def test_get_cover_reaches_on_cover_request(self):
        e = req()
        self.d.handle(e)
        self.replies.on_cover_request.assert_called_once_with(e)

    def test_prev_next_do_not(self):
        for a in ("get_cover_prev", "get_cover_next"):
            with self.assertLogs("mass80d.dispatcher", "DEBUG") as cm:
                self.d.handle(Event(KIND_UNKNOWN, a, raw={"action": a}))
            self.assertIn("no reply", cm.output[0])
        self.replies.on_cover_request.assert_not_called()

    def test_other_unknown_still_only_logs(self):
        with self.assertLogs("mass80d.dispatcher", "DEBUG") as cm:
            self.d.handle(Event(KIND_UNKNOWN, "mystery", raw={"action": "mystery", "x": 1}))
        self.assertIn("ignoring unknown event", cm.output[0])
        self.replies.on_cover_request.assert_not_called()

    def test_handle_never_raises(self):
        self.replies.on_cover_request.side_effect = RuntimeError("x")
        with self.assertLogs("mass80d.dispatcher", "ERROR"):
            self.d.handle(req())

    def test_tick_calls_both_even_when_one_raises(self):
        self.actions.tick.side_effect = RuntimeError("a")
        with self.assertLogs("mass80d.dispatcher", "ERROR"):
            self.d.tick()
        self.replies.tick.assert_called_once_with()
        self.actions.tick.assert_called_once_with()
        self.actions.tick.side_effect = None
        self.replies.tick.side_effect = RuntimeError("r")
        with self.assertLogs("mass80d.dispatcher", "ERROR"):
            self.d.tick()
        self.assertEqual(self.actions.tick.call_count, 2)

    def test_close_never_raises(self):
        self.d.close()
        self.replies.close.assert_called_once_with()
        self.replies.close.side_effect = RuntimeError("x")
        with self.assertLogs("mass80d.dispatcher", "ERROR"):
            self.d.close()

    def test_real_actions_object_still_works(self):
        # Dispatcher with the real Actions and a real ReplyLayer: tick is harmless.
        c = cfg()
        d = Dispatcher(
            Actions(c, CommandRunner(mock.Mock()), Clock()), ReplyLayer(lambda b: None, c)
        )
        d.tick()
        d.close()


class NoRealWorld(unittest.TestCase):
    def setUp(self):
        def boom(*a, **k):
            raise AssertionError("real subprocess or serial open reached")

        for target in ("subprocess.run", "subprocess.Popen", "serial.Serial", "os.system"):
            p = mock.patch(target, side_effect=boom)
            p.start()
            self.addCleanup(p.stop)


class FakeTransport:
    def __init__(self, batches, stop=None, raises=None):
        self.batches = list(batches)
        self.stop = stop
        self.raises = raises
        self.closed = False
        self.order = []

    def read_events(self):
        if self.raises:
            raise self.raises
        if self.batches:
            return self.batches.pop(0)
        if self.stop is not None:
            self.stop.set()
        return []

    def write(self, data):
        raise AssertionError("not expected")

    def close(self):
        self.closed = True


class DaemonLifecycleTest(NoRealWorld):
    def test_run_stops_injected_service_once_on_normal_exit(self):
        svc = FakeService()
        stop = StopFlag()
        t = FakeTransport([[]], stop)
        run(
            cfg(),
            t,
            stop,
            write_enabled=False,
            actions_enabled=False,
            cover_service=svc,
            list_players=lambda: daemon.NO_PLAYER,
        )
        self.assertEqual(svc.stops, [2.0])
        self.assertTrue(t.closed)

    def test_run_stops_service_when_pump_raises(self):
        svc = FakeService()
        stop = StopFlag()
        t = FakeTransport([], stop, raises=RuntimeError("pump"))
        with self.assertRaises(RuntimeError):
            run(
                cfg(),
                t,
                stop,
                write_enabled=False,
                actions_enabled=False,
                cover_service=svc,
                list_players=lambda: daemon.NO_PLAYER,
            )
        self.assertEqual(svc.stops, [2.0])
        self.assertTrue(t.closed)

    def test_dispatcher_close_runs_before_transport_close(self):
        order = []
        svc = FakeService()
        svc.stop = lambda timeout=2.0: order.append("service")
        stop = StopFlag()
        t = FakeTransport([[]], stop)
        t.close = lambda: order.append("transport")
        run(
            cfg(),
            t,
            stop,
            write_enabled=False,
            actions_enabled=False,
            cover_service=svc,
            list_players=lambda: daemon.NO_PLAYER,
        )
        self.assertEqual(order, ["service", "transport"])

    def test_build_dispatcher_start_services_false_creates_no_service(self):
        with mock.patch.object(daemon, "CoverService", side_effect=AssertionError("created")):
            build_dispatcher(
                cfg(),
                lambda b: None,
                lambda a: True,
                list_players=lambda: daemon.NO_PLAYER,
                start_services=False,
            )

    def test_build_dispatcher_creates_and_starts_service_when_enabled(self):
        fake = FakeService()
        fake.start = mock.Mock()
        with mock.patch.object(daemon, "CoverService", return_value=fake) as cls:
            d = build_dispatcher(
                cfg(), lambda b: None, lambda a: True, list_players=lambda: daemon.NO_PLAYER
            )
        cls.assert_called_once()
        self.assertEqual(cls.call_args.args[0], cfg().cover_config())
        fake.start.assert_called_once_with()
        d.close()
        self.assertEqual(fake.stops, [2.0])

    def test_no_service_when_disabled_or_test_image_configured(self):
        with mock.patch.object(daemon, "CoverService", side_effect=AssertionError("created")):
            build_dispatcher(
                default_config(),
                lambda b: None,
                lambda a: True,
                list_players=lambda: daemon.NO_PLAYER,
            )
            build_dispatcher(
                cfg(cover_test_image="x.jpg"),
                lambda b: None,
                lambda a: True,
                list_players=lambda: daemon.NO_PLAYER,
                cover_test_jpeg=JPEG,
            )


class BannerTest(unittest.TestCase):
    ARGS = ("cfg", "/dev/x", True, False, 10.0, "live")

    def test_absent_by_default(self):
        self.assertFalse(any("cover" in line for line in banner(*self.ARGS)))

    def test_normal_mode_line(self):
        lines = banner(
            *self.ARGS, cover=True, cover_size=240, cover_hosts=("i.scdn.co", "b.example")
        )
        text = "\n".join(lines)
        self.assertIn("cover art: ON, size 240x240, fetch https from i.scdn.co, b.example", text)
        self.assertIn("(EXTERNAL network request)", text)
        self.assertIn("local files only from (none)", text)
        lines = banner(*self.ARGS, cover=True, cover_local_dirs=("/a", "/b"))
        self.assertIn("local files only from /a, /b", "\n".join(lines))

    def test_default_canvas_line_is_exactly_the_old_one(self):
        lines = banner(
            *self.ARGS,
            cover=True,
            cover_size=240,
            cover_hosts=("i.scdn.co",),
            cover_local_dirs=("/a",),
            cover_canvas_width=240,
            cover_canvas_height=240,
            cover_fill_grey=99,
        )
        self.assertIn(
            "  cover art: ON, size 240x240, fetch https from i.scdn.co  "
            "(EXTERNAL network request), local files only from /a",
            lines,
        )

    def test_480_square_canvas_line_is_the_square_form(self):
        lines = banner(
            *self.ARGS,
            cover=True,
            cover_size=480,
            cover_hosts=("i.scdn.co",),
            cover_local_dirs=("/a",),
            cover_canvas_width=480,
            cover_canvas_height=480,
            cover_fill_grey=32,
        )
        self.assertIn(
            "  cover art: ON, size 480x480, fetch https from i.scdn.co  "
            "(EXTERNAL network request), local files only from /a",
            lines,
        )

    def test_canvas_line(self):
        lines = banner(
            *self.ARGS,
            cover=True,
            cover_size=240,
            cover_hosts=("i.scdn.co",),
            cover_local_dirs=("/a",),
            cover_canvas_width=480,
            cover_canvas_height=240,
            cover_fill_grey=32,
        )
        self.assertIn(
            "  cover art: ON, size 240x240 on a 480x240 canvas (grey 32), fetch https from "
            "i.scdn.co  (EXTERNAL network request), local files only from /a",
            lines,
        )

    def test_test_mode_line_unchanged_with_a_canvas(self):
        text = "\n".join(
            banner(
                *self.ARGS,
                cover=True,
                cover_test_image="x.jpg",
                cover_test_max_replies=1,
                cover_canvas_width=480,
                cover_fill_grey=7,
            )
        )
        self.assertIn("cover art: TEST IMAGE x.jpg, answers at most 1 request(s), no network", text)
        self.assertNotIn("canvas", text)

    def test_test_mode_line(self):
        text = "\n".join(
            banner(
                *self.ARGS,
                cover=True,
                cover_test_image="configs/assets/cover-test-240.jpg",
                cover_test_max_replies=1,
            )
        )
        self.assertIn(
            "cover art: TEST IMAGE configs/assets/cover-test-240.jpg, answers at most 1 "
            "request(s), no network",
            text,
        )
        self.assertNotIn("EXTERNAL", text)


class LoadTestImageTest(unittest.TestCase):
    def test_accepts_real_asset(self):
        data = _load_cover_test_image(str(ASSET))
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), MAX_COVER_JPEG_BYTES)

    def test_refusals(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.jpg")
            with self.assertRaises(OSError):
                _load_cover_test_image(missing)
            png = os.path.join(d, "a.jpg")
            Path(png).write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)
            with self.assertRaises(ConfigError):
                _load_cover_test_image(png)
            big = os.path.join(d, "big.jpg")
            Path(big).write_bytes(b"\xff\xd8\xff" + b"x" * MAX_COVER_JPEG_BYTES)
            with self.assertRaises(ConfigError):
                _load_cover_test_image(big)
            ok = os.path.join(d, "ok.jpg")
            Path(ok).write_bytes(b"\xff\xd8\xff" + b"x" * (MAX_COVER_JPEG_BYTES - 3))
            self.assertEqual(len(_load_cover_test_image(ok)), MAX_COVER_JPEG_BYTES)

    def test_relative_to_current_directory(self):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(REPO)
        self.assertEqual(
            _load_cover_test_image("configs/assets/cover-test-240.jpg"), ASSET.read_bytes()
        )


class MainBadTestImageTest(NoRealWorld):
    def test_bad_image_exits_3_before_touching_the_port(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "c.toml")
            Path(cfg_path).write_text(
                '[replies]\nget_cover = true\n[cover]\ntest_image = "'
                + os.path.join(d, "no.jpg")
                + '"\n'
            )
            factory = mock.Mock(side_effect=AssertionError("transport built"))
            holders = mock.Mock(side_effect=AssertionError("guard ran"))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(io.StringIO()):
                code = main(
                    ["--config", cfg_path],
                    transport_factory=factory,
                    find_holders=holders,
                    install_signals=False,
                    out=out,
                    err=err,
                )
        self.assertEqual(code, EXIT_BAD_ARGS)
        self.assertIn("mass80d: bad cover test image:", err.getvalue())

    def test_image_not_read_unless_get_cover_enabled(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, "c.toml")
            Path(cfg_path).write_text('[cover]\ntest_image = "missing.jpg"\n')
            out, err = io.StringIO(), io.StringIO()
            code = main(
                ["--config", cfg_path, "--print-config"], out=out, err=err, install_signals=False
            )
        self.assertEqual(code, EXIT_OK)


class PrintConfigStage9fTest(unittest.TestCase):
    def test_print_config_shows_the_canvas_values(self):
        out, err = io.StringIO(), io.StringIO()
        code = main(
            ["--config", str(REPO / "configs" / "stage9f-cover-grey-fill.toml"), "--print-config"],
            out=out,
            err=err,
            install_signals=False,
        )
        self.assertEqual(code, EXIT_OK)
        conf = json.loads(out.getvalue())["config"]
        self.assertEqual(
            (conf["cover_size"], conf["cover_canvas_width"], conf["cover_canvas_height"]),
            (240, 480, 240),
        )
        self.assertEqual(conf["cover_fill_grey"], 49)


class PrintConfigDefaultAndStage9cTest(unittest.TestCase):
    def print_conf(self, args):
        out, err = io.StringIO(), io.StringIO()
        code = main([*args, "--print-config"], out=out, err=err, install_signals=False)
        self.assertEqual(code, EXIT_OK)
        return json.loads(out.getvalue())["config"]

    def test_no_config_file_shows_480_480_480(self):
        conf = self.print_conf([])
        self.assertEqual(
            (conf["cover_size"], conf["cover_canvas_width"], conf["cover_canvas_height"]),
            (480, 480, 480),
        )
        self.assertEqual(conf["cover_fill_grey"], 32)

    def test_stage_9c_shows_240_240_240(self):
        conf = self.print_conf(["--config", str(REPO / "configs" / "stage9c-cover-art.toml")])
        self.assertEqual(
            (conf["cover_size"], conf["cover_canvas_width"], conf["cover_canvas_height"]),
            (240, 240, 240),
        )


class PrintConfigStage11aTest(unittest.TestCase):
    def test_print_config_shows_the_480_values(self):
        out, err = io.StringIO(), io.StringIO()
        code = main(
            ["--config", str(REPO / "configs" / "stage11a-cover-480.toml"), "--print-config"],
            out=out,
            err=err,
            install_signals=False,
        )
        self.assertEqual(code, EXIT_OK)
        conf = json.loads(out.getvalue())["config"]
        self.assertEqual(
            (conf["cover_size"], conf["cover_canvas_width"], conf["cover_canvas_height"]),
            (480, 480, 480),
        )


class WriteGateTruncationTest(unittest.TestCase):
    def test_big_frame_is_truncated_in_the_log_but_forwarded_whole(self):
        sent = []
        frame = b"A" * 14_999 + b"\n"
        gate = WriteGate(True, sent.append)
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            gate(frame)
        self.assertEqual(sent, [frame])
        msg = cm.records[0].getMessage()
        self.assertIn("writing 15000 bytes", msg)
        self.assertLess(len(msg), 400)

    def test_small_frame_log_is_unchanged(self):
        frame = b"x" * 99 + b"\n"
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            WriteGate(True, lambda b: None)(frame)
        self.assertEqual(cm.records[0].getMessage(), f"writing {frame!r}")
        frame512 = b"y" * 511 + b"\n"
        with self.assertLogs("mass80d.daemon", "INFO") as cm:
            WriteGate(True, lambda b: None)(frame512)
        self.assertEqual(cm.records[0].getMessage(), f"writing {frame512!r}")

    def test_suppressed_holds_the_full_frame(self):
        frame = b"A" * 14_999 + b"\n"
        gate = WriteGate(False, None)
        with self.assertLogs("mass80d.daemon", "INFO"):
            gate(frame)
        self.assertEqual(gate.suppressed, [frame])


def _rx(t, obj):
    line = json.dumps(obj) + "\n"
    return json.dumps({"kind": "rx", "t": t, "hex": line.encode().hex()})


class ReplayEndToEndTest(NoRealWorld):
    def write_replay(self, d):
        p = os.path.join(d, "r.log")
        Path(p).write_text(
            "\n".join(
                [
                    _rx(0.0, {"action": "get_music_info"}),
                    _rx(0.6, REQ),
                ]
            )
            + "\n"
        )
        return p

    def run_it(self, config):
        old = os.getcwd()
        self.addCleanup(os.chdir, old)
        os.chdir(REPO)  # the test image path is relative to the current directory
        captured = []
        real = daemon.WriteGate

        def factory(*a, **k):
            g = real(*a, **k)
            captured.append(g)
            return g

        before = threading.enumerate()
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            p = self.write_replay(d)
            with (
                mock.patch.object(daemon, "WriteGate", side_effect=factory),
                mock.patch.object(daemon, "CoverService", side_effect=AssertionError("service")),
                self.assertNoLogs("mass80d", "WARNING"),
            ):
                code = run_replay(config, p, out)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(threading.enumerate(), before)
        return captured[0], out.getvalue()

    def test_stage_9b_replay_would_send_the_test_picture(self):
        config = load_config(str(REPO / "configs" / "stage9b-cover-test.toml"))
        gate, out = self.run_it(config)
        self.assertIn("would-write frames: 2, would-run commands: 0", out)
        self.assertIn("get_cover: 1", out)
        frames = [json.loads(f) for f in gate.suppressed]
        cover = [f for f in frames if f["action"] == "get_cover"]
        self.assertEqual(len(cover), 1)
        self.assertEqual(cover[0]["index"], 0)
        self.assertEqual(
            base64.b64decode(cover[0]["image_data"], validate=True), ASSET.read_bytes()
        )

    def test_get_cover_without_image_gets_null_at_once_and_no_thread(self):
        gate, out = self.run_it(cfg())
        frames = [json.loads(f) for f in gate.suppressed]
        cover = [f for f in frames if f["action"] == "get_cover"]
        self.assertEqual(cover, [{"action": "get_cover", "image_data": "null", "index": 0}])

    def test_stage_9f_replay_gets_null_at_once_and_no_thread(self):
        config = load_config(str(REPO / "configs" / "stage9f-cover-grey-fill.toml"))
        gate, _ = self.run_it(config)
        frames = [json.loads(f) for f in gate.suppressed]
        cover = [f for f in frames if f["action"] == "get_cover"]
        self.assertEqual(cover, [{"action": "get_cover", "image_data": "null", "index": 0}])

    def test_stage_11a_replay_gets_null_at_once_and_no_thread(self):
        config = load_config(str(REPO / "configs" / "stage11a-cover-480.toml"))
        gate, _ = self.run_it(config)
        frames = [json.loads(f) for f in gate.suppressed]
        cover = [f for f in frames if f["action"] == "get_cover"]
        self.assertEqual(cover, [{"action": "get_cover", "image_data": "null", "index": 0}])

    def test_replay_with_default_config_writes_no_cover_frame(self):
        gate, _ = self.run_it(default_config())
        self.assertFalse(any(b"get_cover" in f for f in gate.suppressed))


if __name__ == "__main__":
    unittest.main()
