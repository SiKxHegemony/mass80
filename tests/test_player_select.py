"""T18: pure player selection (choose) and the stateful PlayerSelector. No I/O, no process.

Player names such as `firefox.instance_1_53` are SYNTHETIC: the Firefox instance shape is
expected from `man playerctl`, not observed on this machine (only Spotify was verified).
"""

import itertools
import unittest

from mass80d.player_select import (
    NO_PLAYER,
    UNREADABLE,
    PlayerInfo,
    PlayerListing,
    PlayerSelector,
    choose,
)

PRIO = ("spotify", "firefox")


def pl(instance, status="Playing", name=None, title="T", artist="A", length=1000, pos=0):
    return PlayerInfo(
        instance=instance,
        name=name if name is not None else instance.split(".")[0],
        status=status,
        artist=artist,
        title=title,
        length_us=length,
        position_us=pos,
    )


class PlayerInfoArtUrlTest(unittest.TestCase):
    def test_seven_positional_args_default_art_url_empty(self):
        p = PlayerInfo("a", "b", "Playing", "x", "y", 1, 2)
        self.assertEqual(p.art_url, "")

    def test_art_url_is_the_eighth_field_and_survives_choose(self):
        p = PlayerInfo("a", "b", "Playing", "x", "y", 1, 2, "https://i.scdn.co/x")
        self.assertEqual(p.art_url, "https://i.scdn.co/x")
        self.assertEqual(choose([p], PRIO, None), "a")


class ChooseTest(unittest.TestCase):
    def test_no_players_is_none(self):
        self.assertIsNone(choose([], PRIO, None))
        self.assertIsNone(choose([], PRIO, "spotify"))

    def test_playing_beats_paused(self):
        ps = [pl("spotify", "Paused"), pl("firefox.instance_1_53", "Playing")]
        self.assertEqual(choose(ps, PRIO, None), "firefox.instance_1_53")

    def test_listed_beats_unlisted_among_equals(self):
        ps = [pl("mpv", "Playing"), pl("firefox.instance_1_53", "Playing")]
        self.assertEqual(choose(ps, PRIO, None), "firefox.instance_1_53")

    def test_list_order_among_listed(self):
        ps = [pl("firefox.instance_1_53"), pl("spotify")]
        self.assertEqual(choose(ps, PRIO, None), "spotify")
        self.assertEqual(choose(ps, ("firefox", "spotify"), None), "firefox.instance_1_53")

    def test_alphabetical_among_unlisted_by_player_name(self):
        ps = [pl("vlc.instance9", name="vlc"), pl("chromium.instance3", name="chromium"), pl("mpv")]
        self.assertEqual(choose(ps, PRIO, None), "chromium.instance3")

    def test_playing_unlisted_beats_paused_listed(self):
        ps = [pl("spotify", "Paused"), pl("mpv", "Playing")]
        self.assertEqual(choose(ps, PRIO, None), "mpv")

    def test_playing_status_is_exact(self):
        ps = [pl("spotify", "Stopped"), pl("mpv", "Playing")]
        self.assertEqual(choose(ps, PRIO, None), "mpv")

    def test_nothing_playing_keeps_last_playing_while_it_exists(self):
        ps = [pl("spotify", "Paused"), pl("mpv", "Paused")]
        self.assertEqual(choose(ps, PRIO, "mpv"), "mpv")  # kept although unlisted

    def test_nothing_playing_last_playing_vanished_falls_back(self):
        ps = [pl("mpv", "Paused"), pl("firefox.instance_1_53", "Paused")]
        self.assertEqual(choose(ps, PRIO, "spotify"), "firefox.instance_1_53")

    def test_nothing_playing_no_last_applies_listed_rules(self):
        ps = [pl("mpv", "Paused"), pl("firefox.instance_1_53", "Paused"), pl("spotify", "Paused")]
        self.assertEqual(choose(ps, PRIO, None), "spotify")

    def test_last_playing_ignored_when_something_plays(self):
        ps = [pl("spotify", "Playing"), pl("mpv", "Paused")]
        self.assertEqual(choose(ps, PRIO, "mpv"), "spotify")

    def test_empty_priority_list_all_unlisted_alphabetical(self):
        ps = [pl("spotify"), pl("firefox.instance_1_53")]
        self.assertEqual(choose(ps, (), None), "firefox.instance_1_53")

    def test_prefix_matching(self):
        ps = [pl("firefox.instance_1_53")]
        self.assertEqual(choose(ps, ("firefox",), None), "firefox.instance_1_53")
        # `firefox` matches both instances (list position equal; name then instance decides)
        both = [pl("firefox.instance_9_9"), pl("firefox.instance_1_53"), pl("mpv")]
        self.assertEqual(choose(both, ("firefox",), None), "firefox.instance_1_53")
        # `firefoxy` is NOT matched by the prefix `firefox`: it is unlisted, so mpv (listed) wins
        ps = [pl("firefoxy"), pl("mpv")]
        self.assertEqual(choose(ps, ("mpv",), None), "mpv")
        self.assertEqual(choose([pl("firefoxy"), pl("zzz")], ("firefox",), None), "firefoxy")
        # and a listed firefox beats the unlisted firefoxy
        ps = [pl("firefoxy"), pl("firefox.instance_1_53")]
        self.assertEqual(choose(ps, ("firefox",), None), "firefox.instance_1_53")

    def test_prefix_matches_on_the_player_name_field_too(self):
        ps = [pl("weird.instance_7", name="firefox"), pl("mpv")]
        self.assertEqual(choose(ps, ("firefox",), None), "weird.instance_7")

    def test_instance_name_changes_between_calls(self):
        first = [pl("firefox.instance_1_53")]
        second = [pl("firefox.instance_1_77")]
        self.assertEqual(choose(first, PRIO, None), "firefox.instance_1_53")
        self.assertEqual(choose(second, PRIO, None), "firefox.instance_1_77")

    def test_deterministic_and_independent_of_listing_order(self):
        ps = [
            pl("mpv", "Paused"),
            pl("firefox.instance_1_53", "Playing"),
            pl("firefox.instance_9_9", "Playing"),
            pl("chromium.instance3", "Playing", name="chromium"),
            pl("spotify", "Paused"),
        ]
        expected = choose(ps, PRIO, None)
        self.assertEqual(expected, "firefox.instance_1_53")
        for perm in itertools.permutations(ps):
            self.assertEqual(choose(list(perm), PRIO, None), expected)
            self.assertEqual(choose(list(perm), PRIO, None), expected)  # same inputs, same answer

    def test_input_is_not_mutated(self):
        ps = [pl("b"), pl("a")]
        choose(ps, PRIO, None)
        self.assertEqual([p.instance for p in ps], ["b", "a"])


class Listing:
    """Scripted list_players provider."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        r = self.results[0] if len(self.results) == 1 else self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def listing(*players, skipped=False):
    return PlayerListing(tuple(players), skipped)


class SelectorTest(unittest.TestCase):
    def make(self, *results):
        self.src = Listing(*results)
        return PlayerSelector(self.src, PRIO, clock=lambda: 5.0)

    def test_nothing_runs_at_construction(self):
        self.make(listing(pl("spotify")))
        self.assertEqual(self.src.calls, 0)

    def test_current_returns_chosen_playerinfo(self):
        s = self.make(listing(pl("firefox.instance_1_53", "Paused"), pl("spotify", "Playing")))
        cur = s.current()
        self.assertEqual(cur.instance, "spotify")
        self.assertEqual(self.src.calls, 1)

    def test_every_call_asks_the_provider_again(self):
        s = self.make(listing(pl("spotify")), listing(pl("mpv")))
        self.assertEqual(s.current().instance, "spotify")
        self.assertEqual(s.current().instance, "mpv")  # not cached
        self.assertEqual(self.src.calls, 2)

    def test_empty_listing_is_none_and_no_player(self):
        s = self.make(listing())
        self.assertIsNone(s.current())
        self.assertIs(s.select(), NO_PLAYER)

    def test_outcomes_pass_through(self):
        s = self.make(NO_PLAYER)
        self.assertIs(s.select(), NO_PLAYER)
        self.assertIsNone(s.current())
        s = self.make(UNREADABLE)
        self.assertIs(s.select(), UNREADABLE)
        self.assertIsNone(s.current())

    def test_last_playing_is_remembered_across_calls(self):
        s = self.make(
            listing(pl("spotify", "Paused"), pl("mpv", "Playing")),
            listing(pl("spotify", "Paused"), pl("mpv", "Paused")),
        )
        self.assertEqual(s.current().instance, "mpv")
        self.assertEqual(s.current().instance, "mpv")  # kept: mpv was last seen Playing

    def test_last_playing_dropped_when_player_vanishes(self):
        s = self.make(
            listing(pl("mpv", "Playing"), pl("spotify", "Paused")),
            listing(pl("spotify", "Paused"), pl("firefox.instance_1_53", "Paused")),
            listing(pl("mpv", "Paused"), pl("spotify", "Paused")),
        )
        self.assertEqual(s.current().instance, "mpv")
        self.assertEqual(s.current().instance, "spotify")  # mpv gone: fall back to the list
        # mpv is back but paused: it is no longer the remembered one (it vanished in between)
        self.assertEqual(s.current().instance, "spotify")

    def test_unreadable_keeps_last_playing(self):
        s = self.make(
            listing(pl("mpv", "Playing"), pl("spotify", "Paused")),
            UNREADABLE,
            listing(pl("mpv", "Paused"), pl("spotify", "Paused")),
        )
        s.current()
        self.assertIsNone(s.current())
        self.assertEqual(s.current().instance, "mpv")

    def test_no_player_clears_last_playing(self):
        s = self.make(
            listing(pl("mpv", "Playing")),
            NO_PLAYER,
            listing(pl("mpv", "Paused"), pl("spotify", "Paused")),
        )
        s.current()
        s.current()
        self.assertEqual(s.current().instance, "spotify")

    def test_instance_renamed_between_calls(self):
        s = self.make(
            listing(pl("firefox.instance_1_53", "Playing")),
            listing(pl("firefox.instance_1_77", "Paused")),
        )
        self.assertEqual(s.current().instance, "firefox.instance_1_53")
        # the remembered instance name is gone; falls back by rule, picks the new instance
        self.assertEqual(s.current().instance, "firefox.instance_1_77")

    def test_provider_exception_propagates_for_callers_to_handle(self):
        s = self.make(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            s.current()


if __name__ == "__main__":
    unittest.main()
