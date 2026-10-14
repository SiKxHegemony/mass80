import os
import unittest

from mass80d.config import Binding, ConfigError, default_config, load_config, load_config_text

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO, "config.example.toml")

KEYS = ["Print", "Scroll", "Pause", "Insert", "Home", "Page Up", "Delete", "End", "Page Down"]


class DefaultsTest(unittest.TestCase):
    def test_volume_defaults_are_full_slider_range(self):
        c = default_config()
        self.assertEqual((c.volume_min, c.volume_max), (0, 100))
        self.assertEqual(c.volume_sink, "@DEFAULT_AUDIO_SINK@")
        self.assertGreater(c.volume_min_interval, 0)

    def test_default_keys_are_all_log_only(self):
        c = default_config()
        for k in KEYS:
            self.assertEqual(c.keys[k], Binding("log", ()), k)

    def test_default_light_is_log_and_music_separate(self):
        c = default_config()
        self.assertEqual(c.light_action, "log")
        self.assertEqual(c.music_mode, "separate")

    def test_reply_defaults(self):
        c = default_config()
        self.assertTrue(c.reply_get_time)
        self.assertFalse(c.reply_get_performance)
        self.assertFalse(c.reply_get_music_info)


class ParseTest(unittest.TestCase):
    def test_empty_text_gives_defaults(self):
        self.assertEqual(load_config_text(""), default_config())

    def test_overrides(self):
        c = load_config_text(
            """
[volume]
min = 0
max = 100
min_interval = 0.3
[music]
mode = "toggle"
[keys."Page Up"]
type = "hyprctl"
args = ["workspace", "e-1"]
[replies]
reply_get_performance = true
"""
        )
        self.assertEqual((c.volume_min, c.volume_max, c.volume_min_interval), (0, 100, 0.3))
        self.assertEqual(c.music_mode, "toggle")
        self.assertEqual(c.keys["Page Up"], Binding("hyprctl", ("workspace", "e-1")))
        self.assertEqual(c.keys["Print"], Binding("log", ()))  # untouched keys keep default
        self.assertTrue(c.reply_get_performance)
        self.assertFalse(c.reply_get_music_info)

    def test_rejects_bad_values(self):
        bad = [
            "[volume]\nmin = 50\nmax = 50\n",
            "[volume]\nmin = 60\nmax = 50\n",
            "[volume]\nmin = 'a'\n",
            "[volume]\nmin_interval = -1\n",
            "[music]\nmode = 'shuffle'\n",
            "[light]\naction = 'hyprctl'\n",
            "[keys.Home]\ntype = 'shell'\nargs = ['ls']\n",
            "[keys.Home]\ntype = 'hyprctl'\nargs = 'workspace 1'\n",
            "[keys.Home]\ntype = 'hyprctl'\nargs = [1]\n",
            "[replies]\nreply_get_time = 'yes'\n",
            "[volume]\nbogus = 1\n",
            "[bogus]\nx = 1\n",
        ]
        for text in bad:
            with self.assertRaises(ConfigError, msg=text):
                load_config_text(text)

    def test_invalid_toml(self):
        with self.assertRaises(ConfigError):
            load_config_text("[volume\n")

    def test_load_config_reads_given_path_only(self):
        c = load_config(EXAMPLE)
        self.assertEqual(c.volume_max, 100)


class SerialSectionTest(unittest.TestCase):
    def test_default_silence_timeout_is_10(self):
        self.assertEqual(default_config().serial_silence_timeout, 10.0)

    def test_override_accepts_int_and_float(self):
        self.assertEqual(
            load_config_text("[serial]\nsilence_timeout = 20\n").serial_silence_timeout, 20.0
        )
        self.assertEqual(
            load_config_text("[serial]\nsilence_timeout = 7.5\n").serial_silence_timeout, 7.5
        )

    def test_invalid_values_rejected(self):
        for bad in ("0", "-1", "true", '"10"', "[1]"):
            with self.assertRaises(ConfigError, msg=bad):
                load_config_text(f"[serial]\nsilence_timeout = {bad}\n")

    def test_unknown_key_in_serial_rejected(self):
        with self.assertRaises(ConfigError):
            load_config_text("[serial]\nport = 'x'\n")


class PerformanceConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = default_config()
        self.assertEqual(c.perf_interfaces, ())
        self.assertEqual(c.perf_max_replies, 0)
        self.assertFalse(c.reply_get_performance)

    def test_section_parses(self):
        c = load_config_text(
            '[performance]\ninterfaces = ["eth0", "wlan0", "br-1.2:3_x"]\nmax_replies = 7\n'
        )
        self.assertEqual(c.perf_interfaces, ("eth0", "wlan0", "br-1.2:3_x"))
        self.assertEqual(c.perf_max_replies, 7)
        self.assertFalse(c.reply_get_performance)  # no second switch

    def test_bounds_accepted(self):
        for n in (0, 100):
            self.assertEqual(
                load_config_text(f"[performance]\nmax_replies = {n}\n").perf_max_replies, n
            )
        names = ", ".join(f'"if{i}"' for i in range(8))
        self.assertEqual(
            len(load_config_text(f"[performance]\ninterfaces = [{names}]\n").perf_interfaces), 8
        )
        self.assertEqual(
            load_config_text('[performance]\ninterfaces = ["a23456789012345"]\n').perf_interfaces,
            ("a23456789012345",),
        )

    def test_bad_values_raise_and_name_the_section(self):
        bad = [
            "max_replies = -1",
            "max_replies = 101",
            "max_replies = true",
            'max_replies = "1"',
            "max_replies = 1.5",
            'interfaces = "eth0"',
            'interfaces = [""]',
            'interfaces = ["a b"]',
            'interfaces = [" eth0"]',
            'interfaces = ["a/b"]',
            'interfaces = ["a234567890123456"]',
            "interfaces = [" + ", ".join(f'"i{n}"' for n in range(9)) + "]",
            "interfaces = [1]",
        ]
        for line in bad:
            with self.subTest(line=line):
                with self.assertRaises(ConfigError) as cm:
                    load_config_text(f"[performance]\n{line}\n")
                self.assertIn("[performance]", str(cm.exception))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ConfigError):
            load_config_text("[performance]\nbogus = 1\n")


class ExampleFileTest(unittest.TestCase):
    def test_example_equals_builtin_defaults(self):
        self.assertEqual(load_config(EXAMPLE), default_config())


class MusicPlayersConfigTest(unittest.TestCase):
    def test_default_list(self):
        self.assertEqual(default_config().music_players, ("spotify", "firefox"))
        self.assertEqual(load_config_text("").music_players, ("spotify", "firefox"))

    def test_custom_list_is_a_tuple_in_order(self):
        c = load_config_text('[music]\nplayers = ["mpv", "firefox", "spotify"]\n')
        self.assertEqual(c.music_players, ("mpv", "firefox", "spotify"))
        self.assertEqual(c.music_mode, "separate")

    def test_empty_list_is_allowed(self):
        self.assertEqual(load_config_text("[music]\nplayers = []\n").music_players, ())

    def test_mode_and_players_together(self):
        c = load_config_text('[music]\nmode = "toggle"\nplayers = ["mpv"]\n')
        self.assertEqual((c.music_mode, c.music_players), ("toggle", ("mpv",)))

    def test_invalid_shapes_rejected(self):
        for bad in (
            'players = "spotify"',
            "players = 5",
            "players = [1]",
            'players = ["spotify", 2]',
            'players = [""]',
            'players = [" spotify"]',
            'players = ["spotify "]',
            'players = ["a b "]',
            'players = [["a"]]',
            "players = true",
        ):
            with self.assertRaises(ConfigError, msg=bad):
                load_config_text(f"[music]\n{bad}\n")

    def test_unknown_music_keys_still_rejected(self):
        for key in ("unlisted", "player", "bogus"):
            with self.assertRaises(ConfigError, msg=key):
                load_config_text(f'[music]\n{key} = "ignore"\n')

    def test_print_config_shows_music_players(self):
        import io

        from mass80d.daemon import main

        out = io.StringIO()
        self.assertEqual(main(["--print-config"], out=out, err=io.StringIO()), 0)
        self.assertIn(
            '"music_players": [\n      "spotify",\n      "firefox"\n    ]', out.getvalue()
        )


if __name__ == "__main__":
    unittest.main()


class VolumeQueryConfigTest(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(default_config().reply_volume_query)

    def test_parsed_and_validated(self):
        c = load_config_text("[replies]\nvolume_query = true\n")
        self.assertTrue(c.reply_volume_query)
        self.assertFalse(load_config_text("[replies]\nvolume_query = false\n").reply_volume_query)
        for bad in ("'yes'", "1", "[true]"):
            with self.assertRaises(ConfigError, msg=bad):
                load_config_text(f"[replies]\nvolume_query = {bad}\n")
        with self.assertRaises(ConfigError):
            load_config_text("[replies]\nvolume_quary = true\n")

    def test_range_custom_and_invalid(self):
        c = load_config_text("[volume]\nmin = 4\nmax = 50\n")
        self.assertEqual((c.volume_min, c.volume_max), (4, 50))
        for bad in ("min = 100\nmax = 100", "min = 101\nmax = 100", "min = 0\nmax = 0"):
            with self.assertRaises(ConfigError, msg=bad):
                load_config_text(f"[volume]\n{bad}\n")


class CoverConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = default_config()
        self.assertFalse(c.reply_get_cover)
        self.assertEqual(c.cover_hosts, ("i.scdn.co",))
        self.assertEqual(c.cover_local_dirs, ())
        self.assertEqual((c.cover_size, c.cover_quality), (480, 60))
        self.assertEqual((c.cover_fetch_timeout_s, c.cover_wait_s), (3.0, 3.0))
        self.assertTrue(c.cover_reply_null)
        self.assertEqual((c.cover_test_image, c.cover_test_max_replies), ("", 1))
        self.assertEqual(
            (c.cover_canvas_width, c.cover_canvas_height, c.cover_fill_grey), (480, 480, 32)
        )

    def test_canvas_section_parses(self):
        c = load_config_text(
            "[cover]\nsize = 240\ncanvas_width = 480\ncanvas_height = 240\nfill_grey = 16\n"
        )
        self.assertEqual(
            (c.cover_canvas_width, c.cover_canvas_height, c.cover_fill_grey), (480, 240, 16)
        )
        cc = c.cover_config()
        self.assertEqual((cc.canvas_width, cc.canvas_height, cc.fill_grey), (480, 240, 16))

    def test_size_480_with_480_canvas_parses(self):
        c = load_config_text("[cover]\nsize = 480\ncanvas_width = 480\ncanvas_height = 480\n")
        self.assertEqual(
            (c.cover_size, c.cover_canvas_width, c.cover_canvas_height), (480, 480, 480)
        )
        cc = c.cover_config()
        self.assertEqual((cc.size, cc.canvas_width, cc.canvas_height), (480, 480, 480))

    def test_size_480_needs_the_canvas_and_481_is_refused(self):
        for text in (
            "size = 480\ncanvas_width = 240\ncanvas_height = 240",
            "size = 480\ncanvas_width = 479",
            "size = 480\ncanvas_height = 479",
            "canvas_width = 479",
            "canvas_height = 479",
            "size = 481",
            "size = 481\ncanvas_width = 480\ncanvas_height = 480",
        ):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                load_config_text(f"[cover]\n{text}\n")

    def test_default_is_480_everywhere(self):
        for c in (default_config(), load_config_text(""), load_config(EXAMPLE)):
            self.assertEqual(
                (c.cover_size, c.cover_canvas_width, c.cover_canvas_height), (480,) * 3
            )
            cc = c.cover_config()
            self.assertEqual((cc.size, cc.canvas_width, cc.canvas_height), (480,) * 3)
        self.assertEqual(default_config().cover_fill_grey, 32)

    def test_size_240_alone_is_valid_on_the_480_default_canvas(self):
        c = load_config_text("[cover]\nsize = 240\n")
        self.assertEqual(
            (c.cover_size, c.cover_canvas_width, c.cover_canvas_height), (240, 480, 480)
        )
        c = load_config_text("[cover]\nsize = 240\ncanvas_width = 240\ncanvas_height = 240\n")
        self.assertEqual((c.cover_size, c.cover_canvas_width, c.cover_canvas_height), (240,) * 3)

    def test_bad_canvas_values(self):
        bad = [
            "canvas_width = 0",
            "canvas_width = 481",
            "canvas_width = -1",
            "canvas_width = true",
            "canvas_width = '480'",
            "canvas_height = 0",
            "canvas_height = 481",
            "canvas_height = true",
            "fill_grey = -1",
            "fill_grey = 256",
            "fill_grey = true",
            "fill_grey = 'x'",
            "fill_grey = 1.5",
            "size = 240\ncanvas_width = 100",
            "size = 240\ncanvas_height = 100",
        ]
        for b in bad:
            with self.assertRaises(ConfigError, msg=b):
                load_config_text(f"[cover]\n{b}\n")

    def test_full_section_parses_to_the_right_types(self):
        c = load_config_text(
            "[replies]\nget_cover = true\n[cover]\n"
            'hosts = ["a.example", "b.example"]\nlocal_dirs = ["/x/y"]\nsize = 100\n'
            "quality = 80\nfetch_timeout_s = 2\nwait_s = 4\nreply_null = false\n"
            'test_image = "p/Q.JPEG"\ntest_max_replies = 0\n'
        )
        self.assertTrue(c.reply_get_cover)
        self.assertEqual(c.cover_hosts, ("a.example", "b.example"))
        self.assertEqual(c.cover_local_dirs, ("/x/y",))
        self.assertEqual((c.cover_size, c.cover_quality), (100, 80))
        self.assertIsInstance(c.cover_fetch_timeout_s, float)
        self.assertIsInstance(c.cover_wait_s, float)
        self.assertEqual((c.cover_fetch_timeout_s, c.cover_wait_s), (2.0, 4.0))
        self.assertFalse(c.cover_reply_null)
        self.assertEqual((c.cover_test_image, c.cover_test_max_replies), ("p/Q.JPEG", 0))

    def test_bad_values(self):
        bad = [
            "hosts = 'i.scdn.co'",
            "hosts = [1]",
            'hosts = [""]',
            'hosts = [" a"]',
            'local_dirs = ["rel/dir"]',
            'local_dirs = [""]',
            'local_dirs = ["/ok ", "/x"]',
            "local_dirs = '/x'",
            "size = 0",
            "size = 481",
            "size = true",
            "size = 1.5",
            "quality = 0",
            "quality = 101",
            "fetch_timeout_s = 0",
            "fetch_timeout_s = 11",
            "fetch_timeout_s = true",
            "wait_s = 0",
            "wait_s = 11",
            "wait_s = true",
            "wait_s = 'x'",
            "test_max_replies = -1",
            "test_max_replies = 101",
            "test_max_replies = true",
            "test_max_replies = 1.0",
            "reply_null = 1",
            "test_image = 5",
            'test_image = "a.png"',
            'test_image = "a"',
            "bogus = 1",
        ]
        for b in bad:
            with self.assertRaises(ConfigError, msg=b):
                load_config_text(f"[cover]\n{b}\n")

    def test_jpg_suffix_case_insensitive(self):
        for name in ("a.jpg", "a.JPG", "a.jpeg", "a.JpEg"):
            self.assertEqual(
                load_config_text(f'[cover]\ntest_image = "{name}"\n').cover_test_image, name
            )

    def test_replies_get_cover_validated_and_unknown_key_still_rejected(self):
        with self.assertRaises(ConfigError):
            load_config_text("[replies]\nget_cover = 1\n")
        with self.assertRaises(ConfigError):
            load_config_text("[replies]\nget_covers = true\n")
        with self.assertRaises(ConfigError):
            load_config_text("[covers]\nsize = 1\n")

    def test_cover_config_matches(self):
        from mass80d.cover_art import CoverConfig

        c = load_config_text(
            '[cover]\nhosts = ["h.example"]\nlocal_dirs = ["/d"]\nsize = 120\nquality = 70\n'
            "fetch_timeout_s = 2.5\n"
        )
        cc = c.cover_config()
        self.assertIsInstance(cc, CoverConfig)
        self.assertEqual(
            (cc.hosts, cc.local_dirs, cc.size, cc.quality, cc.fetch_timeout_s),
            (("h.example",), ("/d",), 120, 70, 2.5),
        )
        self.assertEqual(default_config().cover_config(), CoverConfig())
