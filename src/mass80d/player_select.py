"""Player selection: which media player the keyboard screen shows and the buttons control.

Pure logic plus one tiny stateful holder. No I/O here: the listing comes from an injected
callable (in production music_reader.list_players), so tests never start a process.

Rule, in this order of precedence (docs/plans/2026-10-09-player-priority.md section 11):
  1. a Playing player beats one that is not Playing;
  2. among equals, a LISTED player beats an unlisted one;
  3. among listed, the position of the matching entry in the priority list;
  4. among unlisted, alphabetical by player name (then instance name, to be total).
If NO player is Playing: keep the last player seen Playing while it still exists in the
listing, else apply rules 2 to 4 to what exists. No players: no choice.

A priority entry matches a player when it equals the player's name or instance name, or is
that name followed by a dot (playerctl: `firefox` matches `firefox.instance_1_53`). It is
NOT a bare string prefix, so `firefox` does not match `firefoxy`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass


class Outcome(enum.Enum):
    NO_PLAYER = "no player"
    UNREADABLE = "unreadable"


NO_PLAYER = Outcome.NO_PLAYER
UNREADABLE = Outcome.UNREADABLE


@dataclass(frozen=True)
class PlayerInfo:
    """One line of `playerctl -a metadata`. length_us and position_us are microseconds."""

    instance: str  # exact name for `playerctl -p`, e.g. spotify or firefox.instance_1_53
    name: str  # player name, e.g. firefox
    status: str
    artist: str
    title: str
    length_us: int
    position_us: int
    art_url: str = ""  # untrusted text from the player (mpris:artUrl); "" when absent


@dataclass(frozen=True)
class PlayerListing:
    """Every parsed player; `skipped` is True when at least one line could not be parsed."""

    players: tuple[PlayerInfo, ...]
    skipped: bool = False


ListResult = PlayerListing | Outcome


def _matches(player: PlayerInfo, entry: str) -> bool:
    return any(x == entry or x.startswith(entry + ".") for x in (player.instance, player.name))


def _list_position(player: PlayerInfo, priority: Sequence[str]) -> int | None:
    for i, entry in enumerate(priority):
        if _matches(player, entry):
            return i
    return None


def choose(
    players: Iterable[PlayerInfo], priority: Sequence[str], last_playing: str | None
) -> str | None:
    """Return the instance name of the chosen player, or None if there are no players."""
    items = list(players)
    if not items:
        return None

    def key(p: PlayerInfo) -> tuple[bool, bool, int, str, str]:
        pos = _list_position(p, priority)
        return (
            p.status != "Playing",
            pos is None,
            pos if pos is not None else 0,
            p.name,
            p.instance,
        )

    if (
        not any(p.status == "Playing" for p in items)
        and last_playing is not None
        and any(p.instance == last_playing for p in items)
    ):
        return last_playing
    return min(items, key=key).instance


class PlayerSelector:
    """Holds `last_playing` and asks `list_players` every time it is asked (never cached,
    never on a timer). The reply layer and Actions share ONE instance, so the screen and the
    buttons agree."""

    def __init__(
        self,
        list_players: Callable[[], ListResult],
        priority: Sequence[str],
        clock: Callable[[], float],
    ) -> None:
        self._list = list_players
        self._priority = tuple(priority)
        self._clock = clock
        self._last_playing: str | None = None
        self.last_playing_at: float | None = None

    @property
    def priority(self) -> tuple[str, ...]:
        return self._priority

    def select(self) -> PlayerInfo | Outcome:
        """One listing call, then the choice. NO_PLAYER / UNREADABLE pass through."""
        result = self._list()
        if isinstance(result, Outcome):
            if result is NO_PLAYER:
                self._last_playing = None
            return result  # UNREADABLE keeps the remembered player
        players = result.players
        if not players:
            self._last_playing = None
            return NO_PLAYER
        instances = {p.instance for p in players}
        if self._last_playing not in instances:
            self._last_playing = None
        chosen = choose(players, self._priority, self._last_playing)
        if chosen is None:  # unreachable: players is non-empty
            return NO_PLAYER
        info = next(p for p in players if p.instance == chosen)
        if info.status == "Playing":
            self._last_playing = info.instance
            self.last_playing_at = self._clock()
        return info

    def current(self) -> PlayerInfo | None:
        """The chosen player, or None when there is none or the listing was unreadable."""
        r = self.select()
        return r if isinstance(r, PlayerInfo) else None
