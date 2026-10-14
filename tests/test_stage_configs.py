"""T11 Part C: the stage config files load and contain exactly what the plan says."""

import os
import unittest

from mass80d.config import DEFAULT_KEYSYM, KEYSYM_WHITELIST, KNOWN_KEYS, default_config, load_config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def path(name):
    return os.path.join(REPO, "configs", name)


def first_line(p):
    with open(p, encoding="utf-8") as fh:
        return fh.readline()


class Stage3Test(unittest.TestCase):
    def test_values(self):
        p = path("stage3-volume.toml")
        c = load_config(p)
        self.assertEqual(c.volume_max_fraction, 0.3)
        d = default_config()
        self.assertEqual(c.keys, d.keys)  # keys stay log-only
        self.assertEqual(c.music_mode, d.music_mode)
        self.assertEqual(c.reply_get_time, d.reply_get_time)
        self.assertEqual(c.keys_debounce, d.keys_debounce)
        self.assertTrue(first_line(p).startswith("#"))
        self.assertIn("stage 3", first_line(p).lower())

    def test_only_cap_differs_from_defaults(self):
        import dataclasses

        c = load_config(path("stage3-volume.toml"))
        d = dataclasses.replace(default_config(), volume_max_fraction=0.3)
        self.assertEqual(c, d)


class Stage3bTest(unittest.TestCase):
    def test_values(self):
        p = path("stage3b-volume-query.toml")
        c = load_config(p)
        self.assertEqual(c.volume_max_fraction, 0.3)
        self.assertTrue(c.reply_volume_query)
        self.assertEqual((c.volume_min, c.volume_max), (0, 100))
        self.assertTrue(first_line(p).startswith("#"))
        self.assertIn("stage 3b", first_line(p).lower())

    def test_only_cap_and_query_differ_from_defaults(self):
        import dataclasses

        c = load_config(path("stage3b-volume-query.toml"))
        d = dataclasses.replace(default_config(), volume_max_fraction=0.3, reply_volume_query=True)
        self.assertEqual(c, d)


class Stage6aTest(unittest.TestCase):
    def test_values(self):
        import dataclasses

        p = path("stage6a-music-info.toml")
        c = load_config(p)
        self.assertTrue(c.reply_get_music_info)
        self.assertTrue(first_line(p).startswith("#"))
        self.assertIn("stage 6a", first_line(p).lower())
        d = dataclasses.replace(default_config(), reply_get_music_info=True)
        self.assertEqual(c, d)  # only the one reply flag differs from the defaults


class Stage7Test(unittest.TestCase):
    def test_values(self):
        import dataclasses

        p = path("stage7-players.toml")
        c = load_config(p)
        self.assertTrue(c.reply_get_music_info)
        self.assertEqual(c.music_players, ("spotify", "firefox"))
        self.assertTrue(first_line(p).startswith("#"))
        self.assertIn("stage 7", first_line(p).lower())
        d = dataclasses.replace(default_config(), reply_get_music_info=True)
        self.assertEqual(c, d)  # only the one reply flag differs; the list is the default

    def test_players_list_is_written_out_in_the_file(self):
        with open(path("stage7-players.toml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn('players = ["spotify", "firefox"]', text)
        self.assertIn("[music]", text)
        self.assertNotIn("unlisted =", text)


class Stage4bTest(unittest.TestCase):
    def test_values(self):
        p = path("stage4b-keys.toml")
        c = load_config(p)
        self.assertEqual(set(c.keys), set(KNOWN_KEYS))
        for name in KNOWN_KEYS:
            self.assertEqual(c.keys[name].type, "key", name)
            self.assertEqual(c.keys[name].keysym, DEFAULT_KEYSYM[name], name)
            self.assertIn(c.keys[name].keysym, KEYSYM_WHITELIST)
        self.assertTrue(c.reply_get_time)
        self.assertTrue(first_line(p).startswith("#"))
        self.assertIn("stage 4b", first_line(p).lower())

    def test_only_keys_differ_from_defaults(self):
        import dataclasses

        c = load_config(path("stage4b-keys.toml"))
        d = default_config()
        self.assertEqual(dataclasses.replace(c, keys=d.keys), d)

    def test_file_text_has_no_foreign_keysym(self):
        import re

        with open(path("stage4b-keys.toml"), encoding="utf-8") as fh:
            text = fh.read()
        for sym in re.findall(r'keysym\s*=\s*"([^"]*)"', text):
            self.assertIn(sym, KEYSYM_WHITELIST)


def read_text(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class Stage9bTest(unittest.TestCase):
    P = path("stage9b-cover-test.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(c.reply_get_music_info)
        self.assertTrue(c.reply_get_cover)
        self.assertEqual(c.cover_test_image, "configs/assets/cover-test-240.jpg")
        self.assertEqual(c.cover_test_max_replies, 1)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9b", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-test-240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-test-240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("get_cover = true", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-test-240.jpg")
        self.assertTrue(os.path.exists(asset))
        with open(asset, "rb") as fh:
            self.assertEqual(fh.read(3), b"\xff\xd8\xff")


class Stage9eTest(unittest.TestCase):
    P = path("stage9e-cover-grid.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9e", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-grid-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-grid-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-grid-480x240.jpg")
        self.assertTrue(os.path.exists(asset))
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9hTest(unittest.TestCase):
    P = path("stage9h-cover-swatch.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9h", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-swatch-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-swatch-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-swatch-480x240.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9iTest(unittest.TestCase):
    P = path("stage9i-cover-swatch2.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9i", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-swatch2-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-swatch2-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-swatch2-480x240.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9jTest(unittest.TestCase):
    P = path("stage9j-cover-swatch3.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9j", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-swatch3-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-swatch3-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-swatch3-480x240.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9kTest(unittest.TestCase):
    P = path("stage9k-cover-swatch4.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9k", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-swatch4-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-swatch4-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-swatch4-480x240.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9lTest(unittest.TestCase):
    P = path("stage9l-cover-swatch5.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9l", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-swatch5-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-swatch5-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-swatch5-480x240.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9mTest(unittest.TestCase):
    P = path("stage9m-cover-swatch6.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9m", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-swatch6-480x240.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-swatch6-480x240.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-swatch6-480x240.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage9nTest(unittest.TestCase):
    P = path("stage9n-cover-full480.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9n", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_test_image="configs/assets/cover-full-480x480.jpg",
            cover_test_max_replies=1,
        )
        self.assertEqual(c, d)

    def test_text_and_asset(self):
        text = read_text(self.P)
        self.assertIn('test_image = "configs/assets/cover-full-480x480.jpg"', text)
        self.assertIn("test_max_replies = 1", text)
        self.assertIn("--no-actions", text)
        asset = os.path.join(REPO, "configs/assets/cover-full-480x480.jpg")
        with open(asset, "rb") as fh:
            data = fh.read()
        self.assertEqual(data[:3], b"\xff\xd8\xff")
        self.assertLessEqual(len(data), 200_000)


class Stage12bTest(unittest.TestCase):
    P = path("stage12b-performance-run.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 12b", first_line(self.P).lower())
        d = dataclasses.replace(default_config(), reply_get_performance=True)
        self.assertEqual(c, d)
        self.assertEqual(c.perf_max_replies, 0)  # no frame limit
        self.assertFalse(c.reply_get_cover or c.reply_get_music_info or c.reply_volume_query)

    def test_text(self):
        text = read_text(self.P)
        self.assertIn("reply_get_performance = true", text)
        self.assertIn("max_replies = 0", text)
        self.assertIn("--no-actions", text)


class Stage9cTest(unittest.TestCase):
    P = path("stage9c-cover-art.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9c", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_local_dirs=("/home/shane/.zen/firefox-mpris",),
            cover_size=240,
            cover_canvas_width=240,
            cover_canvas_height=240,
        )
        self.assertEqual(c, d)
        self.assertEqual((c.cover_size, c.cover_canvas_width, c.cover_canvas_height), (240,) * 3)
        self.assertEqual(c.cover_test_image, "")  # real art, no test picture

    def test_text(self):
        text = read_text(self.P)
        self.assertIn('local_dirs = ["/home/shane/.zen/firefox-mpris"]', text)
        self.assertIn("i.scdn.co", text)
        self.assertIn("get_cover = true", text)
        self.assertIn("size = 240", text)


class Stage9fTest(unittest.TestCase):
    P = path("stage9f-cover-grey-fill.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 9f", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_local_dirs=("/home/shane/.zen/firefox-mpris",),
            cover_size=240,
            cover_canvas_width=480,
            cover_canvas_height=240,
            cover_fill_grey=49,
        )
        self.assertEqual(c, d)
        self.assertEqual(
            (c.cover_size, c.cover_canvas_width, c.cover_canvas_height, c.cover_fill_grey),
            (240, 480, 240, 49),
        )
        self.assertEqual(c.cover_test_image, "")  # real art, no test picture

    def test_text(self):
        text = read_text(self.P)
        self.assertIn('local_dirs = ["/home/shane/.zen/firefox-mpris"]', text)
        self.assertIn("i.scdn.co", text)
        self.assertIn("get_cover = true", text)
        self.assertIn("reply_get_music_info = true", text)
        self.assertIn("size = 240", text)
        self.assertIn("canvas_width = 480", text)
        self.assertIn("canvas_height = 240", text)
        self.assertIn("fill_grey = 49", text)


class Stage11aTest(unittest.TestCase):
    P = path("stage11a-cover-480.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 11a", first_line(self.P).lower())
        d = dataclasses.replace(
            default_config(),
            reply_get_music_info=True,
            reply_get_cover=True,
            cover_local_dirs=("/home/shane/.zen/firefox-mpris",),
            cover_size=480,
            cover_canvas_width=480,
            cover_canvas_height=480,
        )
        self.assertEqual(c, d)
        self.assertEqual(c.cover_test_image, "")  # real art, no test picture

    def test_text(self):
        text = read_text(self.P)
        self.assertIn('local_dirs = ["/home/shane/.zen/firefox-mpris"]', text)
        self.assertIn("i.scdn.co", text)
        self.assertIn("get_cover = true", text)
        self.assertIn("reply_get_music_info = true", text)
        self.assertIn("size = 480", text)
        self.assertIn("canvas_width = 480", text)
        self.assertIn("canvas_height = 480", text)


class Stage12aTest(unittest.TestCase):
    P = path("stage12a-performance.toml")

    def test_values(self):
        import dataclasses

        c = load_config(self.P)
        self.assertTrue(first_line(self.P).startswith("#"))
        self.assertIn("stage 12a", first_line(self.P).lower())
        d = dataclasses.replace(default_config(), reply_get_performance=True, perf_max_replies=1)
        self.assertEqual(c, d)
        self.assertFalse(c.reply_get_music_info)
        self.assertFalse(c.reply_get_cover)

    def test_text(self):
        text = read_text(self.P)
        self.assertIn("reply_get_performance = true", text)
        self.assertIn("max_replies = 1", text)
        self.assertIn("--no-actions", text)


if __name__ == "__main__":
    unittest.main()
