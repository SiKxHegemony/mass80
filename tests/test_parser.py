import json
import unittest
from collections import Counter
from pathlib import Path

from mass80d.parser import (
    KIND_INPUT,
    KIND_POLL,
    KIND_UNKNOWN,
    EventParser,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_chunks(name):
    chunks = []
    with open(FIXTURES / name, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                chunks.append(bytes.fromhex(json.loads(line)["hex"]))
    return chunks


def parse_all(chunks):
    p = EventParser()
    events = []
    for c in chunks:
        events.extend(p.feed(c))
    return events


class SyntheticStreamTests(unittest.TestCase):
    def test_echo_then_json_emits_once(self):
        data = (
            b'[CDC-TX] ("action":"send_key","key":"Print")\n\r\n'
            b'{"action":"send_key","key":"Print"}\n'
        )
        evs = EventParser().feed(data)
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].action, "send_key")
        self.assertEqual(evs[0].key, "Print")
        self.assertEqual(evs[0].kind, KIND_INPUT)

    def test_echo_alone_is_ignored(self):
        self.assertEqual(EventParser().feed(b'[CDC-TX] ("action":"volume")\n'), [])

    def test_json_after_debug_text_without_newline(self):
        data = ('内部RAM: 已用=378 KB, 剩余=123 KB (最大块=47 KB){"action":"volume"}\n').encode()
        evs = EventParser().feed(data)
        self.assertEqual([e.action for e in evs], ["volume"])
        self.assertIsNone(evs[0].value)

    def test_split_at_every_byte_position(self):
        data = (
            b'[CDC-TX] ("action":"volume","value":42)\n\r\n{"action":"volume","value":42}\n'
            b'{"action":"get_time","version":"2.1.5"}\r\n'
        )
        for i in range(1, len(data)):
            p = EventParser()
            evs = p.feed(data[:i]) + p.feed(data[i:])
            self.assertEqual(
                [(e.action, e.value, e.version) for e in evs],
                [("volume", 42, None), ("get_time", None, "2.1.5")],
                f"split at {i}",
            )

    def test_byte_at_a_time(self):
        data = '[CDC-TX] 剩余\n{"action":"music_play"}\n{"action":"music_pause"}\n'.encode()
        p = EventParser()
        evs = []
        for b in data:
            evs.extend(p.feed(bytes([b])))
        self.assertEqual([e.action for e in evs], ["music_play", "music_pause"])

    def test_crlf_and_lf_terminators(self):
        evs = EventParser().feed(
            b'{"action":"get_time","version":"2.1.5"}\r\n{"action":"get_music_info"}\n'
        )
        self.assertEqual([e.action for e in evs], ["get_time", "get_music_info"])
        self.assertTrue(all(e.kind == KIND_POLL for e in evs))

    def test_incomplete_line_is_held_until_terminator(self):
        p = EventParser()
        self.assertEqual(p.feed(b'{"action":"music_play"}'), [])
        self.assertEqual([e.action for e in p.feed(b"\n")], ["music_play"])

    def test_capture_begins_mid_stream(self):
        evs = EventParser().feed(b'ing=1 KB)[CDC-TX] [C1]foo\n{"action":"light","value":20}\n')
        self.assertEqual([(e.action, e.value) for e in evs], [("light", 20)])

    def test_garbled_debug_and_invalid_utf8_do_not_raise(self):
        data = b'[[CDC-RX] [CDC-TX] ("actio\xff\xfe\x80\n{"action":"music_pause"}\n'
        evs = EventParser().feed(data)
        self.assertEqual([e.action for e in evs], ["music_pause"])

    def test_invalid_utf8_inside_json_does_not_raise(self):
        evs = EventParser().feed(b'{"action":"send_key","key":"\xff"}\n')
        # Must not raise; whatever is emitted must be an event or nothing.
        self.assertLessEqual(len(evs), 1)

    def test_truncated_json_is_dropped_next_line_still_parsed(self):
        evs = EventParser().feed(b'{"action":"vol\n{"action":"music_play"}\n')
        self.assertEqual([e.action for e in evs], ["music_play"])

    def test_music_prev_and_next_are_input_events(self):
        evs = EventParser().feed(b'{"action":"music_prev"}\n{"action":"music_next"}\n')
        self.assertEqual(
            [(e.kind, e.action) for e in evs],
            [(KIND_INPUT, "music_prev"), (KIND_INPUT, "music_next")],
        )

    def test_music_prev_and_next_interleaved_with_debug_text(self):
        data = b'[CDC-TX] x\n{"action":"music_prev"}\n[CDC-RX] y\r\n{"action":"music_next"}\n'
        evs = EventParser().feed(data)
        self.assertEqual([e.action for e in evs], ["music_prev", "music_next"])
        self.assertTrue(all(e.kind == KIND_INPUT for e in evs))

    def test_music_seek_is_still_unknown(self):
        evs = EventParser().feed(b'{"action":"music_seek"}\n')
        self.assertEqual([(e.kind, e.action) for e in evs], [(KIND_UNKNOWN, "music_seek")])

    def test_unknown_action_passes_through(self):
        evs = EventParser().feed(b'{"action":"change_theme","value":3}\n')
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].kind, KIND_UNKNOWN)
        self.assertEqual(evs[0].action, "change_theme")
        self.assertEqual(evs[0].value, 3)
        self.assertEqual(evs[0].raw, {"action": "change_theme", "value": 3})

    def test_json_without_action_is_unknown_not_raised(self):
        evs = EventParser().feed(b'{"foo":1}\n')
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].kind, KIND_UNKNOWN)
        self.assertIsNone(evs[0].action)

    def test_send_key_with_space(self):
        evs = EventParser().feed(b'{"action":"send_key","key":"Page Up"}\n')
        self.assertEqual(evs[0].key, "Page Up")

    def test_volume_marker_vs_value(self):
        evs = EventParser().feed(
            b'{"action":"volume"}\n{"action":"volume","value":30}\n{"action":"light","value":20}\n'
        )
        self.assertTrue(evs[0].is_slider_begin)
        self.assertFalse(evs[1].is_slider_begin)
        self.assertEqual(evs[1].value, 30)
        # Position semantics: value is not clamped to 0-100.
        big = EventParser().feed(b'{"action":"volume","value":250}\n')
        self.assertEqual(big[0].value, 250)

    def test_runaway_line_is_bounded(self):
        p = EventParser(max_line_bytes=1024)
        self.assertEqual(p.feed(b"x" * 5000), [])
        evs = p.feed(b'\n{"action":"music_play"}\n')
        self.assertEqual([e.action for e in evs], ["music_play"])

    def test_reset_drops_partial_line(self):
        p = EventParser()
        p.feed(b'{"action":"volu')
        p.reset()
        self.assertEqual(
            [e.action for e in p.feed(b'me"}\n{"action":"music_play"}\n')], ["music_play"]
        )


class CaptureFixtureTests(unittest.TestCase):
    """Counts are the ones documented in docs/protocol.md; do not adjust them to fit the parser."""

    def check(self, name, expected_actions, expected_volume):
        evs = parse_all(load_chunks(name))
        counts = Counter(e.action for e in evs)
        self.assertEqual(dict(counts), expected_actions)
        vols = [e for e in evs if e.action == "volume"]
        self.assertEqual(sum(1 for e in vols if e.is_slider_begin), expected_volume[0])
        self.assertEqual(sum(1 for e in vols if not e.is_slider_begin), expected_volume[1])
        self.assertFalse([e for e in evs if e.kind == KIND_UNKNOWN])
        return evs

    def test_capture_001911(self):
        evs = self.check(
            "capture_001911.chunks.jsonl",
            {
                "get_music_info": 84,
                "get_time": 7,
                "get_performance": 6,
                "volume": 62,
                "music_play": 1,
                "music_pause": 1,
                "send_key": 9,
            },
            (2, 60),
        )
        keys = [e.key for e in evs if e.action == "send_key"]
        self.assertEqual(
            keys,
            ["Print", "Scroll", "Pause", "Insert", "Home", "Page Up", "Delete", "End", "Page Down"],
        )
        self.assertTrue(all(e.version == "2.1.5" for e in evs if e.action == "get_time"))

    def test_capture_001149(self):
        self.check(
            "capture_001149.chunks.jsonl",
            {
                "get_time": 21,
                "get_music_info": 21,
                "volume": 47,
                "light": 48,
                "send_key": 9,
                "music_play": 1,
                "music_pause": 1,
            },
            (1, 46),
        )

    def test_capture_chunking_independent(self):
        chunks = load_chunks("capture_001911.chunks.jsonl")
        blob = b"".join(chunks)
        whole = [(e.action, e.value, e.key) for e in EventParser().feed(blob)]
        by_chunk = [(e.action, e.value, e.key) for e in parse_all(chunks)]
        p = EventParser()
        odd = []
        for i in range(0, len(blob), 7):
            odd.extend(p.feed(blob[i : i + 7]))
        self.assertEqual(whole, by_chunk)
        self.assertEqual(whole, [(e.action, e.value, e.key) for e in odd])


class DeepNestingTest(unittest.TestCase):
    def test_deeply_nested_line_does_not_raise_and_next_line_parses(self):
        data = b'{"a":' + b"[" * 100000 + b'\n{"action":"music_play"}\n'
        events = EventParser().feed(data)
        self.assertEqual([e.action for e in events], ["music_play"])


if __name__ == "__main__":
    unittest.main()
