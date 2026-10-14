import json
import re
import unittest
from datetime import UTC, datetime, timedelta, timezone

from mass80d.config import default_config, load_config_text
from mass80d.music_reader import NO_PLAYER, UNREADABLE, MusicReading
from mass80d.parser import KIND_POLL, Event
from mass80d.player_select import PlayerInfo, PlayerListing, PlayerSelector
from mass80d.replies import (
    ReplyLayer,
    ReplyRefused,
    ZeroLengthHold,
    build_get_music_info,
    build_get_performance,
    build_get_time,
    build_volume_query_reply,
    filter_sdk_text,
    ms_from_us,
)
from mass80d.transport import TransportError
from mass80d.volume_reader import VolumeReading

EDT = timezone(timedelta(hours=-4))


def poll(action, version=None):
    return Event(KIND_POLL, action, version=version)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class GetTimeFrameTest(unittest.TestCase):
    def test_matches_hardware_verified_frame_exactly(self):
        # Frame taken from the write test that updated the clock
        # (captures/write-test-20261009-004217.log)
        now = datetime(2026, 10, 9, 0, 42, 17, tzinfo=EDT)
        self.assertEqual(
            build_get_time(now),
            b'{"action":"get_time","time":"2026-10-09 00:42:17-04:00","caps":false}\n',
        )

    def test_offset_computed_not_hard_coded(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        self.assertIn(
            b'"2026-01-02 03:04:05+05:30"',
            build_get_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=ist)),
        )
        self.assertIn(
            b'"2026-01-02 03:04:05+00:00"',
            build_get_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)),
        )
        nst = timezone(-timedelta(hours=3, minutes=30))
        self.assertIn(b'-03:30"', build_get_time(datetime(2026, 1, 2, 3, 4, 5, tzinfo=nst)))

    def test_naive_datetime_refused(self):
        with self.assertRaises(ValueError):
            build_get_time(datetime(2026, 1, 2, 3, 4, 5))

    def test_default_uses_local_zone_and_always_has_offset(self):
        frame = build_get_time()
        self.assertTrue(frame.endswith(b"\n"))
        obj = json.loads(frame)
        self.assertRegex(obj["time"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d$")

    def test_caps_flag(self):
        now = datetime(2026, 10, 9, 0, 42, 17, tzinfo=EDT)
        self.assertEqual(json.loads(build_get_time(now, caps=True))["caps"], True)


class GetPerformanceFrameTest(unittest.TestCase):
    def test_shape_and_key_order(self):
        frame = build_get_performance(40, 12, 3400, 45, 10, 200)
        self.assertEqual(
            frame,
            b'{"action":"get_performance","memory_usage":40,"cpu_usage":12,"cpu_freq":3400,'
            b'"cpu_temp":45,"net_upload":10,"net_download":200}\n',
        )

    def test_non_int32_rejected(self):
        for bad in (1.5, True, "5", None, 2**31, -(2**31) - 1):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_get_performance(bad, 0, 0, 0, 0, 0)


class GetMusicInfoFrameTest(unittest.TestCase):
    def test_shape_and_key_order(self):
        frame = build_get_music_info(1000, 200000, "Song", "Band", 0, True)
        self.assertEqual(
            frame,
            b'{"action":"get_music_info","artist":"Band","duration":200000,"index":0,'
            b'"progress":1000,"state":1,"title":"Song"}\n',
        )

    def test_state_false_is_zero(self):
        self.assertEqual(json.loads(build_get_music_info(0, 0, "", "", 0, False))["state"], 0)

    def test_unicode_and_newline_stay_on_one_line(self):
        frame = build_get_music_info(0, 0, "日本\nline", 'a"b', 0, False)
        self.assertEqual(frame.count(b"\n"), 1)
        self.assertEqual(json.loads(frame)["title"], "日本\nline")

    def test_type_checks(self):
        with self.assertRaises(ValueError):
            build_get_music_info(1.5, 0, "", "", 0, False)
        with self.assertRaises(ValueError):
            build_get_music_info(0, 0, 5, "", 0, False)
        with self.assertRaises(ValueError):
            build_get_music_info(0, 0, "", "", 0, 2)


EXAMPLE_FRAME = (
    b'{"action":"get_music_info","artist":"Test Artist","duration":215000,"index":0,'
    b'"progress":42000,"state":1,"title":"Test Title"}\n'
)
EMPTY_FRAME = (
    b'{"action":"get_music_info","artist":"","duration":0,"index":0,'
    b'"progress":0,"state":0,"title":""}\n'
)


class SdkFrameBytesTest(unittest.TestCase):
    def test_example_frame_is_byte_exact(self):
        self.assertEqual(
            build_get_music_info(42000, 215000, "Test Title", "Test Artist", 0, True),
            EXAMPLE_FRAME,
        )

    def test_empty_frame_is_byte_exact(self):
        self.assertEqual(build_get_music_info(0, 0, "", "", 0, False), EMPTY_FRAME)

    def test_keys_alphabetical_and_state_is_integer(self):
        for state in (True, False):
            frame = build_get_music_info(1, 2, "t", "a", 0, state)
            obj = json.loads(frame)
            self.assertEqual(list(obj), sorted(obj))
            self.assertEqual(
                list(obj), ["action", "artist", "duration", "index", "progress", "state", "title"]
            )
            self.assertIs(type(obj["state"]), int)
            self.assertEqual(obj["state"], 1 if state else 0)
            self.assertIn(b'"state":1' if state else b'"state":0', frame)
            self.assertEqual(frame.count(b"\n"), 1)


class FilterTest(unittest.TestCase):
    PUNCT = "~!@#$%^&*(),.;:\"'[]<>{}-_|/ "

    def test_every_allowed_character_passes(self):
        import string

        for ch in self.PUNCT:
            self.assertEqual(filter_sdk_text("a" + ch + "b"), "a" + ch + "b", repr(ch))
        both = string.ascii_letters + string.digits
        self.assertEqual(filter_sdk_text(both), both)
        self.assertEqual(filter_sdk_text(self.PUNCT), self.PUNCT)

    def test_disallowed_ascii_blanks_the_field(self):
        for ch in "?+=\\`":
            self.assertEqual(filter_sdk_text("ab" + ch + "cd"), "", repr(ch))

    def test_other_characters_blank_the_field(self):
        for bad in ("caf\u00e9", "\u30ab\u30bf", "\ud55c", "song \U0001f3b5", "a\x00b", "a\tb"):
            self.assertEqual(filter_sdk_text(bad), "", repr(bad))
        self.assertEqual(filter_sdk_text("a\nb"), "")

    def test_one_bad_character_blanks_the_whole_field_not_a_strip(self):
        self.assertEqual(filter_sdk_text("Hello World, what? A fine day"), "")

    def test_cjk_range_boundaries(self):
        self.assertEqual(filter_sdk_text("\u4e00"), "\u4e00")
        self.assertEqual(filter_sdk_text("\u9fff"), "\u9fff")
        self.assertEqual(filter_sdk_text("\u4dff"), "")
        self.assertEqual(filter_sdk_text("\ua000"), "")
        self.assertEqual(filter_sdk_text("\u65e5\u672c abc"), "\u65e5\u672c abc")

    def test_empty_and_long_text_untouched(self):
        self.assertEqual(filter_sdk_text(""), "")
        self.assertEqual(filter_sdk_text("a" * 5000), "a" * 5000)


class UnitsTest(unittest.TestCase):
    def test_microseconds_to_milliseconds(self):
        self.assertEqual(ms_from_us(187520000), 187520)
        self.assertEqual(ms_from_us(999), 0)
        self.assertEqual(ms_from_us(0), 0)

    def test_clamped_never_raises(self):
        self.assertEqual(ms_from_us(-1), 0)
        self.assertEqual(ms_from_us(-(10**30)), 0)
        self.assertEqual(ms_from_us(10**30), 2**31 - 1)
        self.assertEqual(ms_from_us((2**31) * 1000), 2**31 - 1)
        self.assertEqual(ms_from_us((2**31 - 1) * 1000), 2**31 - 1)


def perf_provider():
    return (40, 12, 3400, 45, 10, 200)


def music_provider():
    return (1000, 200000, "Song", "Band", 0, True)


class ReplyLayerTest(unittest.TestCase):
    def make(self, cfg=None, **kw):
        self.written = []
        self.clock = Clock()
        self.now = datetime(2026, 10, 9, 0, 42, 17, tzinfo=EDT)
        layer = ReplyLayer(
            self.written.append,
            cfg or default_config(),
            clock=self.clock,
            now_func=lambda: self.now,
            **kw,
        )
        return layer

    def test_get_time_answered_once_per_poll(self):
        r = self.make()
        self.assertTrue(r.on_poll(poll("get_time", "2.1.5")))
        self.assertEqual(
            self.written,
            [b'{"action":"get_time","time":"2026-10-09 00:42:17-04:00","caps":false}\n'],
        )

    def test_never_faster_than_floor(self):
        r = self.make()
        self.assertTrue(r.on_poll(poll("get_time")))
        self.clock.t += 0.2  # polls are 2 s apart; this one is a flood
        self.assertFalse(r.on_poll(poll("get_time")))
        self.assertEqual(len(self.written), 1)
        self.clock.t += 2.0
        self.assertTrue(r.on_poll(poll("get_time")))
        self.assertEqual(len(self.written), 2)

    def test_performance_and_music_disabled_by_default(self):
        r = self.make(performance_provider=perf_provider, music_provider=music_provider)
        self.assertFalse(r.on_poll(poll("get_performance")))
        self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [])

    def test_performance_enabled_with_provider(self):
        cfg = load_config_text("[replies]\nreply_get_performance = true\n")
        r = self.make(cfg, performance_provider=perf_provider)
        self.assertTrue(r.on_poll(poll("get_performance")))
        self.assertEqual(json.loads(self.written[0])["action"], "get_performance")

    def test_music_enabled_with_provider(self):
        cfg = load_config_text("[replies]\nreply_get_music_info = true\n")
        r = self.make(cfg, music_provider=music_provider)
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.assertEqual(json.loads(self.written[0])["title"], "Song")

    def test_enabled_without_provider_sends_nothing(self):
        cfg = load_config_text(
            "[replies]\nreply_get_performance = true\nreply_get_music_info = true\n"
        )
        r = self.make(cfg)
        self.assertFalse(r.on_poll(poll("get_performance")))
        self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [])

    def test_provider_returning_none_or_bad_data_sends_nothing(self):
        cfg = load_config_text(
            "[replies]\nreply_get_performance = true\nreply_get_music_info = true\n"
        )
        r = self.make(
            cfg, performance_provider=lambda: None, music_provider=lambda: (1.5, 0, "", "", 0, True)
        )
        self.assertFalse(r.on_poll(poll("get_performance")))
        self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [])

    def test_provider_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("x")

        cfg = load_config_text("[replies]\nreply_get_performance = true\n")
        r = self.make(cfg, performance_provider=boom)
        self.assertFalse(r.on_poll(poll("get_performance")))

    def test_disabled_get_time(self):
        r = self.make(load_config_text("[replies]\nreply_get_time = false\n"))
        self.assertFalse(r.on_poll(poll("get_time")))
        self.assertEqual(self.written, [])

    def test_non_whitelisted_actions_refused(self):
        r = self.make()
        for action in (
            "enter_udisk_mode",
            "change_theme",
            "open_url",
            "boot_report",
            "volume",
            "",
        ):
            frame = json.dumps({"action": action}).encode() + b"\n"
            with self.assertRaises(ReplyRefused, msg=action):
                r._send_frame(action, frame)
        self.assertEqual(self.written, [])

    def test_polls_for_other_actions_never_answered(self):
        r = self.make()
        self.assertFalse(r.on_poll(poll("enter_udisk_mode")))
        self.assertFalse(r.on_poll(Event("input", "volume", 5)))
        self.assertEqual(self.written, [])

    def test_frame_action_must_match_declared_action(self):
        r = self.make()
        with self.assertRaises(ReplyRefused):
            r._send_frame("get_time", b'{"action":"enter_udisk_mode"}\n')
        with self.assertRaises(ReplyRefused):
            r._send_frame("get_time", b"not json\n")
        with self.assertRaises(ReplyRefused):
            r._send_frame("get_time", b'{"action":"get_time"}')  # missing newline
        self.assertEqual(self.written, [])

    def test_no_public_method_accepts_caller_built_frames(self):
        r = self.make()
        self.assertFalse(hasattr(r, "send_frame"))
        public = [n for n in dir(r) if not n.startswith("_") and callable(getattr(r, n))]
        self.assertEqual(
            public, ["close", "on_cover_request", "on_poll", "on_volume_query", "tick"]
        )
        # The only entry point takes an event, so no frame bytes can be supplied.
        self.assertEqual(self.written, [])

    def test_whitelist_is_exactly_five(self):
        from mass80d.replies import REPLY_ACTIONS

        self.assertEqual(
            REPLY_ACTIONS,
            frozenset({"get_time", "get_performance", "get_music_info", "volume", "get_cover"}),
        )

    def test_write_failure_swallowed(self):
        def bad(_):
            raise TransportError("gone")

        r = ReplyLayer(bad, default_config(), clock=Clock(), now_func=lambda: self.now_fixed())
        self.assertFalse(r.on_poll(poll("get_time")))

    def now_fixed(self):
        return datetime(2026, 10, 9, 0, 42, 17, tzinfo=EDT)

    def test_failed_write_does_not_consume_rate_slot(self):
        calls = []

        def flaky(data):
            calls.append(data)
            if len(calls) == 1:
                raise TransportError("gone")

        r = ReplyLayer(flaky, default_config(), clock=Clock(), now_func=self.now_fixed)
        self.assertFalse(r.on_poll(poll("get_time")))
        self.assertTrue(r.on_poll(poll("get_time")))


class VolumeQueryFrameTest(unittest.TestCase):
    def test_bytes_match_decode_document_exactly(self):
        # docs/volume-reply-decode.md, "Recommended first frame"
        self.assertEqual(
            build_volume_query_reply(0), b'{"query":"volume","result":"ok","value":0}\n'
        )
        self.assertEqual(
            build_volume_query_reply(30), b'{"query":"volume","result":"ok","value":30}\n'
        )
        self.assertEqual(
            build_volume_query_reply(100), b'{"query":"volume","result":"ok","value":100}\n'
        )

    def test_one_newline_valid_json_integer_no_action(self):
        for pct in (0, 1, 55, 100):
            frame = build_volume_query_reply(pct)
            self.assertEqual(frame.count(b"\n"), 1)
            self.assertTrue(frame.endswith(b"\n"))
            obj = json.loads(frame)
            self.assertEqual(obj, {"query": "volume", "result": "ok", "value": pct})
            self.assertIs(type(obj["value"]), int)
            self.assertNotIn("action", obj)

    def test_out_of_range_or_non_int_refused(self):
        for bad in (-1, 101, 1.5, "30", None, True, float("nan")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                build_volume_query_reply(bad)


class VolumeQueryLayerTest(unittest.TestCase):
    def make(self, cap=0.3, reading=None, enabled=True):
        self.written, self.clock, self.reads = [], Clock(), 0
        self.reading = reading or VolumeReading(0.30, False)
        flag = "true" if enabled else "false"
        cfg = load_config_text(
            f"[volume]\nmax_fraction = {cap}\n[replies]\nvolume_query = {flag}\n"
        )

        def reader():
            self.reads += 1
            return self.reading

        return ReplyLayer(self.written.append, cfg, clock=self.clock, volume_reader=reader)

    def pct(self):
        return json.loads(self.written[-1])["value"]

    def test_disabled_by_default_writes_and_reads_nothing(self):
        written = []

        def boom():
            raise AssertionError("must not read")

        layer = ReplyLayer(written.append, default_config(), volume_reader=boom)
        self.assertFalse(layer.on_volume_query())
        r = self.make(enabled=False)
        self.assertFalse(r.on_volume_query())
        self.assertEqual((written, self.written, self.reads), ([], [], 0))

    def test_percent_math_cap_03(self):
        for vol, want in ((0.30, 100), (0.15, 50), (0.0, 0), (0.29, 97), (0.98, 100), (1.0, 100)):
            r = self.make(0.3, VolumeReading(vol, False))
            self.assertTrue(r.on_volume_query(), vol)
            self.assertEqual(self.pct(), want, vol)

    def test_percent_math_cap_1(self):
        for vol, want in ((0.30, 30), (0.0, 0), (1.0, 100), (0.555, 56), (1.5, 100)):
            r = self.make(1.0, VolumeReading(vol, False))
            self.assertTrue(r.on_volume_query(), vol)
            self.assertEqual(self.pct(), want, vol)

    def test_rate_limited_to_half_second(self):
        r = self.make()
        self.assertTrue(r.on_volume_query())
        self.clock.t += 0.4
        reads = self.reads
        self.assertFalse(r.on_volume_query())
        self.assertEqual(self.reads, reads)  # a rate-limited marker does not run wpctl
        self.clock.t += 0.11
        self.assertTrue(r.on_volume_query())
        self.assertEqual(len(self.written), 2)

    def test_muted_or_unreadable_writes_nothing_and_logs_once(self):
        for reading in (VolumeReading(0.3, True), VolumeReading(None, False)):
            r = self.make(reading=reading)
            with self.assertLogs("mass80d.replies", "INFO") as cm:
                for _ in range(5):
                    self.assertFalse(r.on_volume_query())
                    self.clock.t += 1
            self.assertEqual(self.written, [])
            self.assertEqual(len(cm.output), 1, cm.output)

    def test_logs_again_after_recovery(self):
        r = self.make(reading=VolumeReading(None, False))
        with self.assertLogs("mass80d.replies", "INFO") as cm:
            r.on_volume_query()
            self.clock.t += 1
            self.reading = VolumeReading(0.3, False)
            self.assertTrue(r.on_volume_query())
            self.clock.t += 1
            self.reading = VolumeReading(None, False)
            r.on_volume_query()
        self.assertEqual(len(cm.output), 2)

    def test_reader_exception_does_not_propagate(self):
        def boom():
            raise RuntimeError("x")

        written = []
        cfg = load_config_text("[replies]\nvolume_query = true\n")
        r = ReplyLayer(written.append, cfg, volume_reader=boom)
        self.assertFalse(r.on_volume_query())
        self.assertEqual(written, [])

    def test_no_reader_writes_nothing(self):
        cfg = load_config_text("[replies]\nvolume_query = true\n")
        written = []
        r = ReplyLayer(written.append, cfg)
        self.assertFalse(r.on_volume_query())
        self.assertEqual(written, [])

    def test_whitelist_still_refuses_other_actions_and_bad_volume_frames(self):
        r = self.make()
        for action in ("enter_udisk_mode", "change_theme", "light", "set_volume", ""):
            with self.assertRaises(ReplyRefused, msg=action):
                r._send_frame(action, json.dumps({"query": action}).encode() + b"\n")
        for frame in (
            b'{"action":"volume","value":30}\n',  # the device-shaped frame, not ours
            b'{"query":"volume","result":"ok","value":30,"action":"volume"}\n',
            b'{"query":"volume","result":"fail","value":30}\n',
            b'{"query":"volume","result":"ok","value":101}\n',
            b'{"query":"volume","result":"ok","value":30.0}\n',
            b'{"query":"volume","result":"ok","value":true}\n',
            b'{"query":"volume","result":"ok","value":30}',
            b"junk\n",
        ):
            with self.assertRaises(ReplyRefused, msg=frame):
                r._send_frame("volume", frame)
        self.assertEqual(self.written, [])

    def test_write_failure_swallowed(self):
        def bad(_):
            raise TransportError("gone")

        cfg = load_config_text("[replies]\nvolume_query = true\n")
        r = ReplyLayer(bad, cfg, clock=Clock(), volume_reader=lambda: VolumeReading(0.5, False))
        self.assertFalse(r.on_volume_query())


class NoDirectWriteTest(unittest.TestCase):
    def test_only_transport_and_nothing_else_calls_serial_write(self):
        import os

        src = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "mass80d"
        )
        for name in os.listdir(src):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(src, name), encoding="utf-8") as fh:
                text = fh.read()
            self.assertIsNone(re.search(r"/dev/tty", text.replace("/dev/serial/by-id", "")), name)
            self.assertNotIn("shell=True", text, name)
            if name not in ("transport.py", "replies.py"):
                self.assertIsNone(re.search(r"\.write\(|self\._write\(", text), name)


class MusicReaderReplyTest(unittest.TestCase):
    CFG = "[replies]\nreply_get_music_info = true\n"

    def make(self, cfg=None, reader=None, write=None):
        self.written = []
        self.clock = Clock()
        self.reads = 0
        self.result = MusicReading("Playing", "Test Artist", "Test Title", 215000000, 42000000)

        def default_reader():
            self.reads += 1
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

        return ReplyLayer(
            write or self.written.append,
            load_config_text(self.CFG) if cfg is None else cfg,
            clock=self.clock,
            music_reader=reader or default_reader,
        )

    def test_disabled_by_default_and_reader_not_called(self):
        r = self.make(default_config())
        self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual((self.written, self.reads), ([], 0))

    def test_playing_sends_the_example_frame(self):
        r = self.make()
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [EXAMPLE_FRAME])

    def test_paused_and_stopped_keep_text_with_state_zero(self):
        r = self.make()
        self.result = MusicReading("Paused", "Test Artist", "Test Title", 215000000, 42000000)
        r.on_poll(poll("get_music_info"))
        self.assertEqual(self.written[-1], EXAMPLE_FRAME.replace(b'"state":1', b'"state":0'))
        self.clock.t += 1
        self.result = MusicReading("Stopped", "Test Artist", "Test Title", 0, 0)
        r.on_poll(poll("get_music_info"))
        self.assertEqual(
            json.loads(self.written[-1]),
            {
                "action": "get_music_info",
                "artist": "Test Artist",
                "duration": 0,
                "index": 0,
                "progress": 0,
                "state": 0,
                "title": "Test Title",
            },
        )

    def test_text_filter_applied_per_field(self):
        r = self.make()
        self.result = MusicReading("Playing", "Bad?", "Good Title", 1000, 0)
        r.on_poll(poll("get_music_info"))
        obj = json.loads(self.written[-1])
        self.assertEqual((obj["artist"], obj["title"]), ("", "Good Title"))

    def test_huge_and_negative_values_do_not_raise(self):
        r = self.make()
        self.result = MusicReading("Playing", "a", "t", -5, 10**30)
        self.assertTrue(r.on_poll(poll("get_music_info")))
        obj = json.loads(self.written[-1])
        self.assertEqual((obj["duration"], obj["progress"]), (0, 2**31 - 1))

    def test_no_player_sends_the_empty_frame_every_poll(self):
        r = self.make()
        self.result = NO_PLAYER
        for _ in range(3):
            self.assertTrue(r.on_poll(poll("get_music_info")))
            self.clock.t += 1.0
        self.assertEqual(self.written, [EMPTY_FRAME] * 3)
        self.assertEqual(self.reads, 3)

    def test_unreadable_sends_nothing_and_logs_once_per_run(self):
        r = self.make()
        self.result = UNREADABLE
        with self.assertLogs("mass80d.replies", "INFO") as cm:
            for _ in range(4):
                self.assertFalse(r.on_poll(poll("get_music_info")))
                self.clock.t += 2.5
        self.assertEqual(self.written, [])
        self.assertEqual(len(cm.output), 1)
        self.result = NO_PLAYER  # recovery, then a new failure logs again
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.clock.t += 2.5
        self.result = UNREADABLE
        with self.assertLogs("mass80d.replies", "INFO") as cm:
            r.on_poll(poll("get_music_info"))
        self.assertEqual(len(cm.output), 1)

    def test_two_second_backoff_after_a_failed_read(self):
        r = self.make()
        self.result = UNREADABLE
        with self.assertLogs("mass80d.replies", "INFO"):
            r.on_poll(poll("get_music_info"))
        self.assertEqual(self.reads, 1)
        self.clock.t += 1.9
        r.on_poll(poll("get_music_info"))
        self.assertEqual(self.reads, 1)  # still inside the back-off
        self.clock.t += 0.2
        r.on_poll(poll("get_music_info"))
        self.assertEqual(self.reads, 2)

    def test_rate_limit_applies_before_the_read(self):
        r = self.make()
        self.assertTrue(r.on_poll(poll("get_music_info")))
        for _ in range(5):
            self.clock.t += 0.1
            self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.reads, 1)
        self.assertEqual(len(self.written), 1)
        self.clock.t += 0.5
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.reads, 2)

    def test_reader_that_raises_does_not_propagate(self):
        r = self.make()
        self.result = RuntimeError("boom")
        with self.assertLogs("mass80d.replies", "ERROR"):
            self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [])

    def test_unexpected_reader_return_value_sends_nothing(self):
        r = self.make(reader=lambda: "not a reading")
        with self.assertLogs("mass80d.replies", "INFO"):
            self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [])

    def test_write_failure_swallowed(self):
        def bad(_):
            raise TransportError("gone")

        r = self.make(write=bad)
        with self.assertLogs("mass80d.replies", "WARNING"):
            self.assertFalse(r.on_poll(poll("get_music_info")))

    def test_other_actions_still_refused(self):
        r = self.make()
        for action in ("enter_udisk_mode", "change_theme"):
            frame = json.dumps({"action": action}).encode() + b"\n"
            with self.assertRaises(ReplyRefused, msg=action):
                r._send_frame(action, frame)
        self.assertEqual(self.written, [])

    def test_get_music_info_frame_still_checked_by_send_frame(self):
        r = self.make()
        with self.assertRaises(ReplyRefused):
            r._send_frame("get_music_info", EXAMPLE_FRAME.rstrip(b"\n"))
        with self.assertRaises(ReplyRefused):
            r._send_frame("get_music_info", b'{"action":"get_time"}\n')


# --- T18: selector-driven music frame and the zero-length hold -----------------------------
# Instance names like firefox.instance_1_53 are SYNTHETIC (not observed on this machine).
def pinfo(instance, status="Playing", title="T", artist="A", length=200_000_000, pos=42_000_000):
    return PlayerInfo(instance, instance.split(".")[0], status, artist, title, length, pos)


class ZeroLengthHoldTest(unittest.TestCase):
    def setUp(self):
        self.t = 100.0
        self.hold = ZeroLengthHold(lambda: self.t)

    def apply(
        self,
        status="Playing",
        length=200_000_000,
        pos=42_000_000,
        inst="spotify",
        title="T",
        artist="A",
    ):
        return self.hold.apply(inst, title, artist, status, length, pos)

    def test_good_reading_passes_through_unchanged(self):
        self.assertEqual(self.apply(), (200_000_000, 42_000_000))

    def test_playing_zero_length_reuses_duration_and_extrapolates(self):
        self.apply()
        self.t += 3.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 45_000_000))
        self.t += 2.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 47_000_000))

    def test_extrapolation_is_clamped_to_duration(self):
        self.apply(pos=199_000_000)
        self.t += 50.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 200_000_000))

    def test_extrapolation_never_negative(self):
        self.apply(pos=0)
        self.t -= 10.0  # a clock that went backwards must not give a negative progress
        length, pos = self.apply(length=0, pos=0)
        self.assertEqual((length, pos), (200_000_000, 0))

    def test_extrapolation_never_exceeds_int32_ms(self):
        big = (2**31 - 1) * 1000 * 4
        self.apply(length=big, pos=big - 1000)
        self.t += 10**9
        length, pos = self.apply(length=0, pos=0)
        self.assertLessEqual(ms_from_us(length), 2**31 - 1)
        self.assertLessEqual(ms_from_us(pos), 2**31 - 1)
        self.assertLessEqual(pos, length)

    def test_reset_on_title_change(self):
        self.apply()
        self.t += 3.0
        self.assertEqual(self.apply(length=0, pos=0, title="Other"), (0, 0))

    def test_reset_on_artist_change(self):
        self.apply()
        self.assertEqual(self.apply(length=0, pos=0, artist="Other"), (0, 0))

    def test_reset_on_instance_change(self):
        self.apply()
        self.assertEqual(self.apply(length=0, pos=0, inst="firefox.instance_1_53"), (0, 0))

    def test_changed_key_does_not_come_back_to_old_values(self):
        self.apply()
        self.apply(length=0, pos=0, title="Other")
        self.assertEqual(self.apply(length=0, pos=0), (0, 0))  # old track's values were dropped

    def test_paused_zero_length_is_sent_unchanged(self):
        self.apply()
        self.t += 3.0
        self.assertEqual(self.apply(status="Paused", length=0, pos=0), (0, 0))

    def test_no_earlier_good_values_sends_what_the_player_reports(self):
        self.assertEqual(self.apply(length=0, pos=0), (0, 0))
        self.assertEqual(self.apply(length=0, pos=7_000_000), (0, 7_000_000))

    def test_playing_resumes_from_paused_position_not_from_the_pause_time(self):
        self.apply(pos=10_000_000)  # good, Playing at t=100
        self.t += 5.0
        self.apply(status="Paused", length=0, pos=0)  # paused at t=105: position 15 s frozen
        self.t += 60.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 15_000_000))
        self.t += 1.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 16_000_000))

    def test_paused_good_reading_then_playing_zero(self):
        self.apply(status="Paused", pos=30_000_000)
        self.t += 40.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 30_000_000))

    def test_new_good_reading_replaces_the_anchor(self):
        self.apply(pos=10_000_000)
        self.t += 5.0
        self.apply(pos=90_000_000)  # the user seeked
        self.t += 1.0
        self.assertEqual(self.apply(length=0, pos=0), (200_000_000, 91_000_000))

    def test_reset_method_drops_the_values(self):
        self.apply()
        self.hold.reset()
        self.assertEqual(self.apply(length=0, pos=0), (0, 0))

    def test_unknown_instance_none_is_a_valid_key(self):
        self.hold.apply(None, "T", "A", "Playing", 10_000_000, 0)
        self.t += 1.0
        self.assertEqual(self.hold.apply(None, "T", "A", "Playing", 0, 0), (10_000_000, 1_000_000))


class SelectorReplyTest(unittest.TestCase):
    CFG = "[replies]\nreply_get_music_info = true\n"
    PRIO = ("spotify", "firefox")

    def make(self, *results):
        self.written = []
        self.clock = Clock()
        self.calls = 0
        self.results = list(results)

        def provider():
            self.calls += 1
            r = self.results[0] if len(self.results) == 1 else self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self.selector = PlayerSelector(provider, self.PRIO, self.clock)
        return ReplyLayer(
            self.written.append,
            load_config_text(self.CFG),
            clock=self.clock,
            music_selector=self.selector,
        )

    def last(self):
        return json.loads(self.written[-1])

    def test_nothing_runs_at_construction(self):
        self.make(PlayerListing((pinfo("spotify"),)))
        self.assertEqual(self.calls, 0)

    def test_frame_is_from_the_selected_player_not_the_first_listed(self):
        r = self.make(
            PlayerListing(
                (
                    pinfo("firefox.instance_1_53", "Paused", "Vid", "Chan"),
                    pinfo("spotify", "Playing", "Song", "Band"),
                )
            )
        )
        self.assertTrue(r.on_poll(poll("get_music_info")))
        obj = self.last()
        self.assertEqual((obj["title"], obj["artist"], obj["state"]), ("Song", "Band", 1))
        self.assertEqual(self.calls, 1)

    def test_playing_unlisted_beats_paused_listed_on_the_screen(self):
        r = self.make(
            PlayerListing((pinfo("spotify", "Paused", "Song"), pinfo("mpv", "Playing", "Clip")))
        )
        r.on_poll(poll("get_music_info"))
        self.assertEqual(self.last()["title"], "Clip")

    def test_no_players_gives_the_empty_frame(self):
        r = self.make(NO_PLAYER)
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [EMPTY_FRAME])
        r = self.make(PlayerListing(()))
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [EMPTY_FRAME])

    def test_unreadable_sends_nothing_logs_once_and_backs_off(self):
        r = self.make(UNREADABLE)
        with self.assertLogs("mass80d.replies", "INFO") as cm:
            for _ in range(2):
                self.assertFalse(r.on_poll(poll("get_music_info")))
                self.clock.t += 0.9
        self.assertEqual(self.written, [])
        self.assertEqual(len(cm.output), 1)
        self.assertEqual(self.calls, 1)  # second poll inside the 2 s back-off: no listing
        self.clock.t += 1.2
        r.on_poll(poll("get_music_info"))  # same run of failures: no second log line
        self.assertEqual(self.calls, 2)

    def test_rate_limit_applies_before_the_listing(self):
        r = self.make(PlayerListing((pinfo("spotify"),)))
        self.assertTrue(r.on_poll(poll("get_music_info")))
        for _ in range(4):
            self.clock.t += 0.1
            self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.calls, 1)
        self.clock.t += 1.0
        self.assertTrue(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.calls, 2)

    def test_selector_that_raises_is_contained(self):
        r = self.make(RuntimeError("boom"))
        with self.assertLogs("mass80d.replies", "ERROR"):
            self.assertFalse(r.on_poll(poll("get_music_info")))
        self.assertEqual(self.written, [])

    def test_zero_length_playing_reuses_duration_and_extrapolates(self):
        good = PlayerListing((pinfo("spotify", "Playing", length=200_000_000, pos=42_000_000),))
        zero = PlayerListing((pinfo("spotify", "Playing", length=0, pos=0),))
        r = self.make(good, zero, zero)
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (200000, 42000))
        self.clock.t += 3.0
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (200000, 45000))
        self.clock.t += 400.0
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (200000, 200000))

    def test_zero_length_reset_on_title_change_and_instance_change(self):
        good = PlayerListing((pinfo("spotify", "Playing", "One"),))
        other_title = PlayerListing((pinfo("spotify", "Playing", "Two", length=0, pos=0),))
        other_inst = PlayerListing(
            (pinfo("firefox.instance_1_53", "Playing", "Two", length=0, pos=0),)
        )
        r = self.make(good, other_title, other_inst)
        r.on_poll(poll("get_music_info"))
        self.clock.t += 2.0
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (0, 0))
        self.clock.t += 2.0
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (0, 0))

    def test_instance_change_with_same_title_resets(self):
        good = PlayerListing((pinfo("firefox.instance_1_53", "Playing", "Same"),))
        moved = PlayerListing((pinfo("firefox.instance_1_77", "Playing", "Same", length=0, pos=0),))
        r = self.make(good, moved)
        r.on_poll(poll("get_music_info"))
        self.clock.t += 2.0
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (0, 0))

    def test_paused_zero_length_stays_zero(self):
        good = PlayerListing((pinfo("spotify", "Playing"),))
        paused = PlayerListing((pinfo("spotify", "Paused", length=0, pos=0),))
        r = self.make(good, paused)
        r.on_poll(poll("get_music_info"))
        self.clock.t += 2.0
        r.on_poll(poll("get_music_info"))
        obj = self.last()
        self.assertEqual((obj["duration"], obj["progress"], obj["state"]), (0, 0, 0))

    def test_no_earlier_good_values_sends_zero(self):
        r = self.make(PlayerListing((pinfo("spotify", "Playing", length=0, pos=0),)))
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["duration"], self.last()["progress"]), (0, 0))

    def test_text_filter_unchanged_on_selected_player(self):
        r = self.make(PlayerListing((pinfo("spotify", "Playing", "What?", "Band"),)))
        r.on_poll(poll("get_music_info"))
        self.assertEqual((self.last()["title"], self.last()["artist"]), ("", "Band"))


if __name__ == "__main__":
    unittest.main()
