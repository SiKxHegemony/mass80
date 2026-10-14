"""T17: read-only `playerctl metadata` reader. Every subprocess is a fake."""

import subprocess
import unittest
from unittest import mock

from mass80d.music_reader import (
    NO_PLAYER,
    UNREADABLE,
    MusicReader,
    MusicReading,
    parse_listing,
    parse_metadata,
)
from mass80d.player_select import PlayerInfo, PlayerListing

SEP = "\x1f"


def line(status="Playing", artist="A", title="T", length="187520000", position="42000000"):
    return SEP.join([status, artist, title, length, position]) + "\n"


class FakeRun:
    def __init__(self, stdout=b"", stderr=b"", returncode=0, exc=None):
        self.calls = []
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.exc = exc

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


def read(**kw):
    return MusicReader(FakeRun(**kw))()


class ParseTest(unittest.TestCase):
    def test_playing(self):
        self.assertEqual(
            parse_metadata(line(), ""), MusicReading("Playing", "A", "T", 187520000, 42000000)
        )

    def test_paused(self):
        self.assertEqual(parse_metadata(line(status="Paused"), "").status, "Paused")

    def test_stopped_with_empty_fields(self):
        r = parse_metadata(SEP.join(["Stopped", "", "", "", ""]) + "\n", "")
        self.assertEqual(r, MusicReading("Stopped", "", "", 0, 0))

    def test_empty_length_only_is_zero(self):
        r = parse_metadata(line(length=""), "")
        self.assertEqual((r.length_us, r.position_us), (0, 42000000))

    def test_pipe_and_question_mark_and_unicode_survive_parsing(self):
        for title in ("A | B", "What?", "日本語 café 🎵", "a  b ", " lead"):
            r = parse_metadata(line(title=title), "")
            self.assertEqual(r.title, title, repr(title))

    def test_only_one_trailing_newline_removed(self):
        r = parse_metadata(line(title="x\n"), "")  # a title that itself ends in a newline
        self.assertEqual(r.title, "x\n")

    def test_no_players_found_in_stdout_or_stderr(self):
        self.assertIs(parse_metadata("No players found\n", ""), NO_PLAYER)
        self.assertIs(parse_metadata("", "No players found\n"), NO_PLAYER)

    def test_garbage_and_wrong_field_count(self):
        for text in (
            "",
            "\n",
            "garbage",
            SEP.join(["Playing", "a", "b", "1"]) + "\n",  # 4 fields
            SEP.join(["Playing", "a", "b", "1", "2", "3"]) + "\n",  # 6 fields
        ):
            self.assertIs(parse_metadata(text, ""), UNREADABLE, repr(text))

    def test_non_numeric_length_or_position(self):
        for bad in ("abc", "1.5", "12x", " ", "1_0", "٣"):
            self.assertIs(parse_metadata(line(length=bad), ""), UNREADABLE, repr(bad))
            self.assertIs(parse_metadata(line(position=bad), ""), UNREADABLE, repr(bad))

    def test_negative_numbers_parse_for_the_clamp_to_handle(self):
        r = parse_metadata(line(length="-5", position="-7"), "")
        self.assertEqual((r.length_us, r.position_us), (-5, -7))


class ReaderTest(unittest.TestCase):
    def test_argv_is_a_list_without_shell_and_read_only(self):
        run = FakeRun(line().encode())
        res = MusicReader(run)()
        self.assertEqual(res, MusicReading("Playing", "A", "T", 187520000, 42000000))
        argv, kw = run.calls[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:3], ["playerctl", "metadata", "--format"])
        self.assertEqual(len(argv), 4)
        for field in ("status", "artist", "title", "mpris:length", "position"):
            self.assertIn("{{" + field + "}}", argv[3])
        self.assertEqual(argv[3].count(SEP), 4)
        self.assertNotIn("-p", argv)
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["timeout"], 2)
        self.assertTrue(kw["capture_output"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["check"], False)

    def test_no_players_stdout_exit0_stdout_exit1_and_stderr(self):
        self.assertIs(read(stdout=b"No players found\n"), NO_PLAYER)
        self.assertIs(read(stdout=b"No players found\n", returncode=1), NO_PLAYER)
        self.assertIs(read(stderr=b"No players found\n"), NO_PLAYER)
        self.assertIs(read(stderr=b"No players found\n", returncode=1), NO_PLAYER)

    def test_nonzero_exit_is_unreadable(self):
        self.assertIs(read(stdout=line().encode(), returncode=1), UNREADABLE)

    def test_bad_output_is_unreadable(self):
        self.assertIs(read(stdout=b"junk"), UNREADABLE)
        self.assertIs(read(stdout=b"\xff\xfe"), UNREADABLE)
        self.assertIs(read(stdout=None), UNREADABLE)

    def test_str_stdout_accepted(self):
        self.assertIsInstance(read(stdout=line()), MusicReading)

    def test_missing_binary_timeout_and_other_errors(self):
        for exc in (
            FileNotFoundError("playerctl"),
            PermissionError("x"),
            subprocess.TimeoutExpired("playerctl", 2),
            subprocess.SubprocessError("x"),
            OSError("x"),
            ValueError("x"),
            RuntimeError("something unexpected"),
        ):
            self.assertIs(read(exc=exc), UNREADABLE, repr(exc))

    def test_default_run_is_subprocess_run_with_list_argv(self):
        with mock.patch("mass80d.music_reader.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess([], 0, line().encode(), b"")
            res = MusicReader()()
        self.assertIsInstance(res, MusicReading)
        self.assertEqual(m.call_args.args[0][:3], ["playerctl", "metadata", "--format"])


# --- T18: listing mode (`playerctl -a metadata`, 7 fields) -------------------------------
# The Firefox instance name `firefox.instance_1_53` is SYNTHETIC (shape expected from
# `man playerctl`, not observed here); only `spotify` was seen on this machine.
def pline(
    instance="spotify",
    name="spotify",
    status="Playing",
    artist="A",
    title="T",
    length="187520000",
    position="42000000",
    art="",
):
    return SEP.join([status, artist, title, length, position, instance, name, art]) + "\n"


def pline7(instance="spotify", name="spotify"):
    """Legacy 7-field line (no artUrl field)."""
    return SEP.join(["Playing", "A", "T", "187520000", "42000000", instance, name]) + "\n"


def info(
    instance="spotify",
    name="spotify",
    status="Playing",
    artist="A",
    title="T",
    ln=187520000,
    pos=42000000,
    art_url="",
):
    return PlayerInfo(instance, name, status, artist, title, ln, pos, art_url)


def lst(**kw):
    return MusicReader(FakeRun(**kw)).list_players()


class ListingParseTest(unittest.TestCase):
    def test_one_player(self):
        self.assertEqual(parse_listing(pline(), ""), PlayerListing((info(),), False))

    def test_three_players_in_listing_order(self):
        text = (
            pline("firefox.instance_1_53", "firefox", "Paused", "Band", "Vid", "0", "0")
            + pline()
            + pline("mpv", "mpv", "Stopped", "", "", "", "")
        )
        r = parse_listing(text, "")
        self.assertEqual(
            [p.instance for p in r.players], ["firefox.instance_1_53", "spotify", "mpv"]
        )
        self.assertFalse(r.skipped)
        self.assertEqual(r.players[0].name, "firefox")
        self.assertEqual(r.players[0].status, "Paused")

    def test_pipe_in_title_survives(self):
        r = parse_listing(pline(title="A | B | C"), "")
        self.assertEqual(r.players[0].title, "A | B | C")

    def test_wrong_field_count_is_skipped_and_flagged_but_others_kept(self):
        for count in (6, 9):
            with self.subTest(fields=count):
                bad = SEP.join(["Playing", "a", "b", "1", "2", "spotify", "x", "", "y"][:count])
                r = parse_listing(pline("mpv", "mpv") + bad + "\n" + pline(), "")
                self.assertEqual([p.instance for p in r.players], ["mpv", "spotify"])
                self.assertTrue(r.skipped)

    def test_eight_fields_give_art_url(self):
        url = "https://i.scdn.co/image/ab67616d0000b273abc"
        r = parse_listing(pline(art=url), "")
        self.assertEqual(r.players[0].art_url, url)
        self.assertEqual(r.players[0], info(art_url=url))

    def test_empty_art_field_is_fine(self):
        r = parse_listing(pline(art=""), "")
        self.assertEqual(r.players[0].art_url, "")
        self.assertFalse(r.skipped)

    def test_seven_fields_still_accepted_with_empty_art(self):
        r = parse_listing(pline7(), "")
        self.assertEqual(r, PlayerListing((info(),), False))
        self.assertEqual(r.players[0].art_url, "")

    def test_seven_and_eight_field_lines_mix(self):
        r = parse_listing(pline7("mpv", "mpv") + pline(art="file:///a.png"), "")
        self.assertEqual([p.art_url for p in r.players], ["", "file:///a.png"])
        self.assertFalse(r.skipped)

    def test_playerinfo_seven_positional_args_defaults_art(self):
        p = PlayerInfo("a", "b", "Playing", "x", "y", 1, 2)
        self.assertEqual(p.art_url, "")

    def test_non_numeric_length_line_is_skipped_others_kept(self):
        r = parse_listing(pline(length="abc") + pline(), "")
        self.assertEqual(len(r.players), 1)
        self.assertTrue(r.skipped)

    def test_empty_length_and_position_are_zero(self):
        r = parse_listing(pline(length="", position=""), "")
        self.assertEqual((r.players[0].length_us, r.players[0].position_us), (0, 0))

    def test_unicode(self):
        r = parse_listing(pline(title="日本語 café 🎵", artist="Ünï"), "")
        self.assertEqual((r.players[0].title, r.players[0].artist), ("日本語 café 🎵", "Ünï"))

    def test_no_players_found(self):
        self.assertIs(parse_listing("", "No players found\n"), NO_PLAYER)
        self.assertIs(parse_listing("No players found\n", ""), NO_PLAYER)

    def test_all_lines_bad_is_unreadable(self):
        self.assertIs(parse_listing("garbage\n", ""), UNREADABLE)
        self.assertIs(parse_listing("", ""), UNREADABLE)

    def test_blank_lines_ignored(self):
        r = parse_listing("\n" + pline() + "\n", "")
        self.assertEqual(len(r.players), 1)
        self.assertFalse(r.skipped)

    def test_negative_numbers_parse(self):
        r = parse_listing(pline(length="-5", position="-7"), "")
        self.assertEqual((r.players[0].length_us, r.players[0].position_us), (-5, -7))


class ListPlayersTest(unittest.TestCase):
    def test_argv_is_exactly_playerctl_a_metadata_format_template_no_shell(self):
        from mass80d.music_reader import LIST_FORMAT

        run = FakeRun(pline().encode())
        res = MusicReader(run).list_players()
        self.assertEqual(res, PlayerListing((info(),), False))
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["playerctl", "-a", "metadata", "--format", LIST_FORMAT])
        self.assertEqual(len(run.calls), 1)
        self.assertEqual(
            LIST_FORMAT,
            SEP.join(
                [
                    "{{status}}",
                    "{{artist}}",
                    "{{title}}",
                    "{{mpris:length}}",
                    "{{position}}",
                    "{{playerInstance}}",
                    "{{playerName}}",
                    "{{mpris:artUrl}}",
                ]
            ),
        )
        self.assertTrue(LIST_FORMAT.endswith(SEP + "{{mpris:artUrl}}"))
        self.assertFalse(kw.get("shell", False))
        self.assertEqual(kw["timeout"], 2)
        self.assertTrue(kw["capture_output"])
        self.assertEqual(kw["stdin"], subprocess.DEVNULL)
        self.assertIs(kw["check"], False)

    def test_single_player_reader_argv_unchanged(self):
        run = FakeRun(line().encode())
        MusicReader(run)()
        self.assertEqual(run.calls[0][0][:3], ["playerctl", "metadata", "--format"])

    def test_no_players_stderr_exit1_stdout_exit1_exit0(self):
        self.assertIs(lst(stderr=b"No players found\n", returncode=1), NO_PLAYER)
        self.assertIs(lst(stdout=b"No players found\n", returncode=1), NO_PLAYER)
        self.assertIs(lst(stdout=b"No players found\n"), NO_PLAYER)

    def test_nonzero_exit_with_garbage_is_unreadable(self):
        self.assertIs(lst(stdout=b"garbage", stderr=b"boom", returncode=2), UNREADABLE)
        self.assertIs(lst(stdout=pline().encode(), returncode=1), UNREADABLE)

    def test_bad_bytes_and_none(self):
        self.assertIs(lst(stdout=b"\xff\xfe"), UNREADABLE)
        self.assertIs(lst(stdout=None), UNREADABLE)

    def test_missing_binary_timeout_and_other_errors(self):
        for exc in (
            FileNotFoundError("playerctl"),
            PermissionError("x"),
            subprocess.TimeoutExpired("playerctl", 2),
            subprocess.SubprocessError("x"),
            OSError("x"),
            ValueError("x"),
            RuntimeError("a runner that raises something unexpected"),
        ):
            self.assertIs(lst(exc=exc), UNREADABLE, repr(exc))

    def test_str_stdout_accepted(self):
        self.assertIsInstance(lst(stdout=pline()), PlayerListing)

    def test_default_run_is_subprocess_run(self):
        with mock.patch("mass80d.music_reader.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess([], 0, pline().encode(), b"")
            res = MusicReader().list_players()
        self.assertIsInstance(res, PlayerListing)
        self.assertEqual(m.call_args.args[0][:4], ["playerctl", "-a", "metadata", "--format"])

    def test_outcome_objects_are_shared_with_player_select(self):
        from mass80d import player_select

        self.assertIs(NO_PLAYER, player_select.NO_PLAYER)
        self.assertIs(UNREADABLE, player_select.UNREADABLE)


if __name__ == "__main__":
    unittest.main()
