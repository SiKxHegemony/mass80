import subprocess
import unittest
from unittest import mock

from mass80d.actions import Actions, CommandRunner, volume_fraction
from mass80d.config import Binding, default_config, load_config_text

# T11 added a default volume cap of 0.5; tests of the unclamped mapping lift it.
NO_CAP = load_config_text("[volume]\nmax_fraction = 1.0\n")


class FakeRun:
    """Stand-in for subprocess.run. Records calls; never starts a process."""

    def __init__(self, exc=None, returncode=0):
        self.calls = []
        self.exc = exc
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, b"", b"")

    @property
    def argvs(self):
        return [c[0] for c in self.calls]


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


def make(cfg=None):
    run = FakeRun()
    clock = Clock()
    return Actions(cfg or default_config(), CommandRunner(run), clock), run, clock


class VolumeFractionTest(unittest.TestCase):
    def test_endpoints_and_middle(self):
        self.assertEqual(volume_fraction(4, 4, 50), 0.0)
        self.assertEqual(volume_fraction(50, 4, 50), 1.0)
        self.assertEqual(volume_fraction(27, 4, 50), 0.5)

    def test_clamps_outside_range(self):
        self.assertEqual(volume_fraction(0, 4, 50), 0.0)
        self.assertEqual(volume_fraction(-5, 4, 50), 0.0)
        self.assertEqual(volume_fraction(99, 4, 50), 1.0)

    def test_custom_range(self):
        self.assertEqual(volume_fraction(30, 0, 100), 0.3)

    def test_rounded_to_percent(self):
        self.assertEqual(volume_fraction(10, 4, 50), 0.13)


class CommandRunnerTest(unittest.TestCase):
    def test_argument_list_no_shell(self):
        run = FakeRun()
        self.assertTrue(CommandRunner(run)(["wpctl", "status"]))
        argv, kw = run.calls[0]
        self.assertEqual(argv, ["wpctl", "status"])
        self.assertFalse(kw.get("shell", False))
        self.assertIn("timeout", kw)

    def test_default_uses_subprocess_run_without_shell(self):
        with mock.patch("mass80d.actions.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess(["x"], 0, b"", b"")
            CommandRunner()(["playerctl", "play"])
        args, kw = m.call_args
        self.assertEqual(args[0], ["playerctl", "play"])
        self.assertFalse(kw.get("shell", False))

    def test_failures_are_swallowed_and_reported_false(self):
        self.assertFalse(CommandRunner(FakeRun(exc=FileNotFoundError("no")))(["playerctl", "play"]))
        self.assertFalse(CommandRunner(FakeRun(exc=subprocess.TimeoutExpired("x", 1)))(["x"]))
        self.assertFalse(CommandRunner(FakeRun(returncode=1))(["x"]))


class RescaleTest(unittest.TestCase):
    def sent(self, cap, value):
        a, run, _ = make(load_config_text(f"[volume]\nmax_fraction = {cap}\n"))
        a.volume(value)
        return run.argvs[-1][-1] if run.argvs else None

    def test_exact_fractions(self):
        table = {
            (0.3, 0): "0.0", (0.3, 50): "0.15", (0.3, 100): "0.3",
            (0.5, 0): "0.0", (0.5, 50): "0.25", (0.5, 100): "0.5",
            (1.0, 0): "0.0", (1.0, 50): "0.5", (1.0, 100): "1.0",
        }  # fmt: skip
        for (cap, value), want in table.items():
            self.assertEqual(self.sent(cap, value), want, (cap, value))

    def test_cap_never_exceeded_for_any_input(self):
        for cap in (0.01, 0.3, 0.5, 0.99, 1.0):
            a, run, clock = make(load_config_text(f"[volume]\nmax_fraction = {cap}\n"))
            for v in range(-1000, 1001):
                a.volume(v)
                clock.t += 1
            for junk in (None, "30", True, False, 3.5, float("nan"), float("inf"), [1], {}):
                a.volume(junk)
                clock.t += 1
            self.assertTrue(run.argvs)
            for argv in run.argvs:
                self.assertEqual(argv[:3], ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@"])
                self.assertLessEqual(float(argv[3]), cap, (cap, argv))
                self.assertGreaterEqual(float(argv[3]), 0.0)


class VolumeTest(unittest.TestCase):
    def test_first_value_runs_immediately_with_fraction(self):
        a, run, clock = make()
        a.volume(100)  # default cap 0.5: slider maximum is exactly the cap
        self.assertEqual(run.argvs, [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.5"]])

    def test_absolute_not_delta(self):
        # gesture A ends at 42, gesture B starts at 30: result must be 30's fraction, not 42-ish
        a, run, clock = make(NO_CAP)
        a.volume(42)
        clock.t += 1
        a.volume(30)
        self.assertEqual(run.argvs[-1][-1], "0.3")

    def test_out_of_range_is_clamped(self):
        a, run, clock = make(NO_CAP)
        a.volume(500)
        self.assertEqual(run.argvs[-1][-1], "1.0")
        clock.t += 1
        a.volume(-9)
        self.assertEqual(run.argvs[-1][-1], "0.0")

    def test_rate_limited_and_coalesced(self):
        a, run, clock = make()
        a.volume(10)  # t=100.0 runs
        for v in (11, 12, 13, 14):  # within interval: coalesced
            clock.t += 0.01
            a.volume(v)
        self.assertEqual(len(run.argvs), 1)
        clock.t += 0.2
        a.tick()
        self.assertEqual(len(run.argvs), 2)
        self.assertEqual(run.argvs[-1][-1], str(round(volume_fraction(14, 0, 100) * 0.5, 2)))
        clock.t += 1
        a.tick()  # nothing pending
        self.assertEqual(len(run.argvs), 2)

    def test_ten_events_per_second_stream_spawns_few_processes(self):
        a, run, clock = make()
        for i in range(50):  # 5 s at 10 events/s
            a.volume(5 + i % 40)
            clock.t += 0.1
        self.assertLessEqual(len(run.argvs), 35)
        self.assertLess(len(run.argvs), 50)

    def test_unchanged_fraction_not_resent(self):
        a, run, clock = make()
        a.volume(27)
        clock.t += 1
        a.volume(27)
        self.assertEqual(len(run.argvs), 1)

    def test_failed_command_is_retried_on_next_event(self):
        run = FakeRun(returncode=1)
        clock = Clock()
        a = Actions(default_config(), CommandRunner(run), clock)
        a.volume(27)
        clock.t += 1
        a.volume(27)
        self.assertEqual(len(run.argvs), 2)

    def test_failed_final_value_is_retried_by_tick(self):
        run = FakeRun(returncode=1)
        clock = Clock()
        a = Actions(default_config(), CommandRunner(run), clock)
        a.volume(27)
        self.assertEqual(len(run.argvs), 1)
        run.returncode = 0  # wpctl recovers; no new slider event arrives
        clock.t += Actions.RETRY_BACKOFF_S + 0.01
        a.tick()
        self.assertEqual(len(run.argvs), 2)
        self.assertEqual(run.argvs[1], run.argvs[0])
        clock.t += 10
        a.tick()  # success clears pending: nothing more is sent
        self.assertEqual(len(run.argvs), 2)

    def test_persistent_failure_spawns_at_most_one_process_per_backoff(self):
        run = FakeRun(returncode=1)
        clock = Clock()
        a = Actions(default_config(), CommandRunner(run), clock)
        a.volume(27)
        for _ in range(500):  # 5 s of ticks every 10 ms
            clock.t += 0.01
            a.tick()
        # 1 initial + one retry per backoff interval inside 5 s
        self.assertLessEqual(len(run.argvs), 1 + int(5.0 / Actions.RETRY_BACKOFF_S))
        self.assertGreaterEqual(len(run.argvs), 2)

    def test_bad_values_ignored(self):
        a, run, clock = make()
        for bad in (None, "30", True, 3.5, [1]):
            a.volume(bad)
        self.assertEqual(run.argvs, [])

    def test_custom_range_and_sink(self):
        cfg = load_config_text(
            '[volume]\nmin=0\nmax=100\nsink="@DEFAULT_AUDIO_SINK@"\nmax_fraction=1.0\n'
        )
        a, run, clock = make(cfg)
        a.volume(30)
        self.assertEqual(run.argvs[-1], ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "0.3"])


class MusicTest(unittest.TestCase):
    def test_separate_mode(self):
        a, run, _ = make()
        a.music("music_play")
        a.music("music_pause")
        self.assertEqual(run.argvs, [["playerctl", "play"], ["playerctl", "pause"]])

    def test_toggle_mode(self):
        a, run, _ = make(load_config_text('[music]\nmode = "toggle"\n'))
        a.music("music_play")
        a.music("music_pause")
        self.assertEqual(run.argvs, [["playerctl", "play-pause"]] * 2)

    def test_prev_next_argv_in_both_modes(self):
        for cfg in (default_config(), load_config_text('[music]\nmode = "toggle"\n')):
            a, run, _ = make(cfg)
            a.music("music_prev")
            self.assertEqual(run.argvs, [["playerctl", "previous"]])
            a, run, _ = make(cfg)
            a.music("music_next")
            self.assertEqual(run.argvs, [["playerctl", "next"]])

    def test_four_actions_in_sequence_separate_mode(self):
        a, run, _ = make()
        for x in ("music_play", "music_pause", "music_prev", "music_next"):
            a.music(x)
        self.assertEqual(
            run.argvs,
            [
                ["playerctl", "play"],
                ["playerctl", "pause"],
                ["playerctl", "previous"],
                ["playerctl", "next"],
            ],
        )

    def test_four_actions_in_sequence_toggle_mode(self):
        a, run, _ = make(load_config_text('[music]\nmode = "toggle"\n'))
        for x in ("music_play", "music_pause", "music_prev", "music_next"):
            a.music(x)
        self.assertEqual(
            run.argvs,
            [
                ["playerctl", "play-pause"],
                ["playerctl", "play-pause"],
                ["playerctl", "previous"],
                ["playerctl", "next"],
            ],
        )

    # Changed in R11: this used to assert that "music_next" is ignored. music_next is now
    # handled, so the test uses actions that are still unknown.
    def test_unknown_music_action_ignored(self):
        a, run, _ = make()
        for x in ("music_seek", "music_stop", "", None, 5):
            a.music(x)  # type: ignore[arg-type]
        self.assertEqual(run.argvs, [])

    def test_failing_runner_does_not_raise_for_prev_next(self):
        run = FakeRun(returncode=1)
        a = Actions(default_config(), CommandRunner(run), Clock())
        a.music("music_prev")
        a.music("music_next")
        self.assertEqual(run.argvs, [["playerctl", "previous"], ["playerctl", "next"]])


class KeyAndLightTest(unittest.TestCase):
    def test_default_keys_run_nothing(self):
        a, run, _ = make()
        for k in [
            "Print",
            "Scroll",
            "Pause",
            "Insert",
            "Home",
            "Page Up",
            "Delete",
            "End",
            "Page Down",
        ]:
            a.send_key(k)
        self.assertEqual(run.argvs, [])

    def test_unknown_or_missing_key_runs_nothing(self):
        a, run, _ = make()
        a.send_key("F13")
        a.send_key(None)
        self.assertEqual(run.argvs, [])

    def test_hyprctl_binding(self):
        cfg = load_config_text('[keys."Page Up"]\ntype="hyprctl"\nargs=["workspace","e-1"]\n')
        a, run, _ = make(cfg)
        a.send_key("Page Up")
        self.assertEqual(run.argvs, [["hyprctl", "dispatch", "workspace", "e-1"]])

    def test_playerctl_binding(self):
        cfg = load_config_text('[keys.Home]\ntype="playerctl"\nargs=["next"]\n')
        a, run, _ = make(cfg)
        a.send_key("Home")
        self.assertEqual(run.argvs, [["playerctl", "next"]])

    def test_binding_helper_default_log(self):
        self.assertEqual(Binding("log", ()).type, "log")

    def test_light_is_noop(self):
        a, run, _ = make()
        a.light(30)
        a.light(None)
        self.assertEqual(run.argvs, [])


# --- T18: buttons follow the shared selector -----------------------------------------------
# firefox.instance_1_53 is a SYNTHETIC instance name (not observed on this machine).
from mass80d.player_select import (  # noqa: E402
    NO_PLAYER,
    UNREADABLE,
    PlayerInfo,
    PlayerListing,
    PlayerSelector,
)

BUTTONS = ("music_play", "music_pause", "music_prev", "music_next")


def pi(instance, status="Playing"):
    return PlayerInfo(instance, instance.split(".")[0], status, "A", "T", 1000, 0)


class SelectorActionsTest(unittest.TestCase):
    def make(self, *results, cfg=None):
        self.calls = 0
        self.results = list(results)

        def provider():
            self.calls += 1
            r = self.results[0] if len(self.results) == 1 else self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        self.selector = PlayerSelector(provider, ("spotify", "firefox"), Clock())
        run = FakeRun()
        a = Actions(cfg or default_config(), CommandRunner(run), Clock(), selector=self.selector)
        return a, run

    TWO = PlayerListing((pi("firefox.instance_1_53", "Paused"), pi("spotify", "Playing")))

    def test_four_buttons_with_two_players_separate_mode(self):
        a, run = self.make(self.TWO)
        for x in BUTTONS:
            a.music(x)
        self.assertEqual(
            run.argvs,
            [
                ["playerctl", "-p", "spotify", "play"],
                ["playerctl", "-p", "spotify", "pause"],
                ["playerctl", "-p", "spotify", "previous"],
                ["playerctl", "-p", "spotify", "next"],
            ],
        )

    def test_four_buttons_with_two_players_toggle_mode(self):
        a, run = self.make(self.TWO, cfg=load_config_text('[music]\nmode = "toggle"\n'))
        for x in BUTTONS:
            a.music(x)
        self.assertEqual(
            run.argvs,
            [
                ["playerctl", "-p", "spotify", "play-pause"],
                ["playerctl", "-p", "spotify", "play-pause"],
                ["playerctl", "-p", "spotify", "previous"],
                ["playerctl", "-p", "spotify", "next"],
            ],
        )

    def test_exact_instance_name_is_used_not_the_prefix(self):
        a, run = self.make(PlayerListing((pi("firefox.instance_1_53", "Playing"),)))
        a.music("music_next")
        self.assertEqual(run.argvs, [["playerctl", "-p", "firefox.instance_1_53", "next"]])

    def test_button_goes_to_the_same_player_the_frame_would_show(self):
        listing = PlayerListing((pi("firefox.instance_1_53", "Paused"), pi("mpv", "Playing")))
        a, run = self.make(listing)
        a.music("music_pause")
        shown = self.selector.current()
        self.assertEqual(run.argvs[-1][2], shown.instance)
        self.assertEqual(shown.instance, "mpv")  # playing unlisted beats paused listed

    def test_selector_is_asked_at_press_time(self):
        a, run = self.make(
            PlayerListing((pi("spotify", "Playing"),)),
            PlayerListing((pi("spotify", "Paused"), pi("mpv", "Playing"))),
        )
        a.music("music_next")
        a.music("music_next")
        self.assertEqual(self.calls, 2)
        self.assertEqual(
            run.argvs,
            [["playerctl", "-p", "spotify", "next"], ["playerctl", "-p", "mpv", "next"]],
        )

    def test_nothing_runs_at_construction(self):
        self.make(self.TWO)
        self.assertEqual(self.calls, 0)

    def test_no_player_runs_nothing_and_logs_once_per_run(self):
        a, run = self.make(NO_PLAYER)
        with self.assertLogs("mass80d.actions", "INFO") as cm:
            for x in BUTTONS * 2:
                a.music(x)
        self.assertEqual(run.argvs, [])
        self.assertEqual(len(cm.output), 1)

    def test_unreadable_listing_runs_nothing(self):
        a, run = self.make(UNREADABLE)
        with self.assertLogs("mass80d.actions", "INFO"):
            a.music("music_play")
        self.assertEqual(run.argvs, [])

    def test_log_rearms_after_a_player_is_found_again(self):
        a, run = self.make(NO_PLAYER, PlayerListing((pi("spotify"),)), NO_PLAYER)
        with self.assertLogs("mass80d.actions", "INFO") as cm:
            a.music("music_next")
        self.assertEqual(len(cm.output), 1)
        a.music("music_next")
        self.assertEqual(run.argvs, [["playerctl", "-p", "spotify", "next"]])
        with self.assertLogs("mass80d.actions", "INFO") as cm:
            a.music("music_next")
        self.assertEqual(len(cm.output), 1)

    def test_selector_that_raises_runs_nothing_and_does_not_raise(self):
        a, run = self.make(RuntimeError("boom"))
        with self.assertLogs("mass80d.actions", "ERROR"):
            a.music("music_next")
        self.assertEqual(run.argvs, [])

    def test_unknown_action_does_not_ask_the_selector(self):
        a, run = self.make(self.TWO)
        for x in ("music_seek", "", None, 5):
            a.music(x)  # type: ignore[arg-type]
        self.assertEqual((self.calls, run.argvs), (0, []))

    def test_no_actions_gate_logs_and_runs_nothing(self):
        gated = []
        selector = PlayerSelector(lambda: self.TWO, ("spotify",), Clock())
        a = Actions(
            default_config(), lambda argv: gated.append(argv) or True, Clock(), selector=selector
        )
        a.music("music_play")
        self.assertEqual(gated, [["playerctl", "-p", "spotify", "play"]])

    def test_without_a_selector_the_old_argvs_are_kept(self):
        for cfg in (default_config(), load_config_text('[music]\nmode = "toggle"\n')):
            a, run, _ = make(cfg)
            for x in BUTTONS:
                a.music(x)
            self.assertNotIn("-p", [t for argv in run.argvs for t in argv])
        a, run, _ = make()
        for x in BUTTONS:
            a.music(x)
        self.assertEqual(
            run.argvs,
            [["playerctl", c] for c in ("play", "pause", "previous", "next")],
        )


if __name__ == "__main__":
    unittest.main()
