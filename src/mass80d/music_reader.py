"""Read-only music reader: runs `playerctl metadata --format <template>` and parses the answer.

Like volume_reader this changes NOTHING on the system, so it deliberately does not go through
the ActionGate (`--no-actions` still means "no system change"). It needs the command's
stdout, which CommandRunner does not return, so it has its own injectable `run` (default
subprocess.run). It never raises, never uses a shell, and gives up after 2 seconds.

Three outcomes (see `ReadResult`):
  MusicReading  a player answered: status, artist, title, length_us, position_us.
  NO_PLAYER     playerctl says "No players found" (stdout or stderr; the exit code varies).
  UNREADABLE    anything else that went wrong: non-zero exit, timeout, OSError, missing
                binary, wrong field count, non-numeric length/position.

The five fields are joined by the ASCII unit separator (0x1f) so that a `|` or any other
ordinary character in a title cannot break parsing. Default player (no `-p`).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .player_select import NO_PLAYER, UNREADABLE, Outcome, PlayerInfo, PlayerListing

log = logging.getLogger("mass80d.music_reader")

READ_TIMEOUT_S = 2
SEP = "\x1f"
FORMAT = SEP.join(("{{status}}", "{{artist}}", "{{title}}", "{{mpris:length}}", "{{position}}"))
FIELD_COUNT = 5
# Listing mode (T18, T21): the same five fields plus the exact instance name (for `-p`), the
# player name and the art URL (untrusted text; cover_art validates it), one line per player.
LIST_FORMAT = SEP.join((FORMAT, "{{playerInstance}}", "{{playerName}}", "{{mpris:artUrl}}"))
LIST_FIELD_COUNT = 8
# The pre-T21 shape without the art URL is still accepted (art_url == "").
LIST_FIELD_COUNT_LEGACY = 7
_NO_PLAYERS = "No players found"
_INT = re.compile(r"-?[0-9]+", re.ASCII)


@dataclass(frozen=True)
class MusicReading:
    """length_us and position_us are microseconds as playerctl reports them (may be 0)."""

    status: str
    artist: str
    title: str
    length_us: int
    position_us: int


__all__ = [
    "NO_PLAYER",
    "UNREADABLE",
    "LIST_FORMAT",
    "MusicReader",
    "MusicReading",
    "Outcome",
    "ReadResult",
    "parse_listing",
    "parse_metadata",
]

ReadResult = MusicReading | Outcome  # Outcome, NO_PLAYER, UNREADABLE live in player_select


def _micros(field: str) -> int | None:
    """Empty (a stopped player) is 0; a plain integer is itself; anything else is None."""
    if field == "":
        return 0
    if _INT.fullmatch(field):
        return int(field)
    return None


def parse_metadata(stdout: str, stderr: str) -> ReadResult:
    if _NO_PLAYERS in stdout or _NO_PLAYERS in stderr:
        return NO_PLAYER
    text = stdout[:-1] if stdout.endswith("\n") else stdout  # exactly one terminator
    fields = text.split(SEP)
    if len(fields) != FIELD_COUNT:
        return UNREADABLE
    status, artist, title, length, position = fields
    length_us, position_us = _micros(length), _micros(position)
    if length_us is None or position_us is None:
        return UNREADABLE
    return MusicReading(status, artist, title, length_us, position_us)


def parse_listing(stdout: str, stderr: str) -> PlayerListing | Outcome:
    """Parse `playerctl -a metadata` output, one 8-field line per player
    (a legacy 7-field line without the art URL is also accepted).

    A line that does not parse is skipped and sets `skipped`; the good lines are kept.
    NO_PLAYER if playerctl says so; UNREADABLE if there were lines but none parsed, or the
    output was empty.
    """
    if _NO_PLAYERS in stdout or _NO_PLAYERS in stderr:
        return NO_PLAYER
    players: list[PlayerInfo] = []
    skipped = False
    for raw in stdout.split("\n"):
        if raw == "":
            continue
        fields = raw.split(SEP)
        if len(fields) == LIST_FIELD_COUNT_LEGACY:
            fields.append("")
        if len(fields) != LIST_FIELD_COUNT:
            skipped = True
            continue
        status, artist, title, length, position, instance, name, art_url = fields
        length_us, position_us = _micros(length), _micros(position)
        if length_us is None or position_us is None or instance == "":
            skipped = True
            continue
        players.append(
            PlayerInfo(instance, name, status, artist, title, length_us, position_us, art_url)
        )
    if not players:
        return UNREADABLE
    return PlayerListing(tuple(players), skipped)


def _text(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, (bytes, bytearray)):
        return bytes(data).decode("utf-8")
    return str(data)


class MusicReader:
    def __init__(self, run: Callable[..., Any] | None = None) -> None:
        self._run = run  # None -> subprocess.run, resolved at call time

    def __call__(self) -> ReadResult:
        run = self._run or subprocess.run
        argv = ["playerctl", "metadata", "--format", FORMAT]
        try:
            res = run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=READ_TIMEOUT_S,
                check=False,
            )
            out = _text(res.stdout)
            err = _text(res.stderr)
            parsed = parse_metadata(out, err)
            if parsed is NO_PLAYER:
                return NO_PLAYER  # recognised by its text, whatever the exit code was
            if res.returncode != 0:
                log.debug("playerctl metadata exited %s", res.returncode)
                return UNREADABLE
            return parsed
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.debug("playerctl metadata failed: %s", exc)
            return UNREADABLE
        except Exception:  # never raise into the dispatcher
            log.exception("unexpected error reading the music player")
            return UNREADABLE

    def list_players(self) -> PlayerListing | Outcome:
        """ONE read-only `playerctl -a metadata --format <LIST_FORMAT>`. Never raises."""
        run = self._run or subprocess.run
        argv = ["playerctl", "-a", "metadata", "--format", LIST_FORMAT]
        try:
            res = run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=READ_TIMEOUT_S,
                check=False,
            )
            out = _text(res.stdout)
            err = _text(res.stderr)
            if _NO_PLAYERS in out or _NO_PLAYERS in err:
                return NO_PLAYER  # recognised by its text, whatever the exit code was
            if res.returncode != 0:
                log.debug("playerctl -a metadata exited %s", res.returncode)
                return UNREADABLE
            return parse_listing(out, err)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            log.debug("playerctl -a metadata failed: %s", exc)
            return UNREADABLE
        except Exception:  # never raise into the callers
            log.exception("unexpected error listing the music players")
            return UNREADABLE
