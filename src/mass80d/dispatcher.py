"""Dispatcher: routes parser events to the action layer and the reply layer."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from .actions import Actions
from .parser import KIND_INPUT, KIND_POLL, Event
from .replies import ReplyLayer

log = logging.getLogger("mass80d.dispatcher")


MAX_PAYLOAD_LOG_CHARS = 300  # an unknown event may carry a large blob; never log all of it


def _payload_text(event: Event) -> str:
    """The raw JSON of an unknown event, truncated, for the DEBUG log. Never raises."""
    try:
        text = json.dumps(event.raw, ensure_ascii=False, sort_keys=True, default=repr)
    except Exception:  # a log helper must not be able to stop the daemon
        return "(payload not printable)"
    if len(text) > MAX_PAYLOAD_LOG_CHARS:
        return f"{text[:MAX_PAYLOAD_LOG_CHARS]}... (truncated, {len(text)} chars)"
    return text


class Dispatcher:
    def __init__(self, actions: Actions, replies: ReplyLayer) -> None:
        self._actions = actions
        self._replies = replies

    def handle(self, event: Event) -> None:
        """Route one event. Never raises: one bad event must not stop the daemon."""
        try:
            if event.kind == KIND_POLL:
                self._replies.on_poll(event)
            elif event.kind == KIND_INPUT:
                self._input(event)
            elif event.action == "get_cover":
                # Always logged first: the reply layer only logs a request it rejects.
                log.debug("get_cover request %s", _payload_text(event))
                self._replies.on_cover_request(event)
            elif event.action in ("get_cover_prev", "get_cover_next"):
                log.debug(
                    "%s request %s: the SDK sends no reply, ignoring",
                    event.action,
                    _payload_text(event),
                )
            else:
                log.debug("ignoring unknown event %r %s", event.action, _payload_text(event))
        except Exception:
            log.exception("error handling %r", event.action)

    def _input(self, event: Event) -> None:
        a = event.action
        if a == "volume":
            if event.value is None:
                self._replies.on_volume_query()  # bare marker = the device asks for the volume
            self._actions.volume(event.value)  # begin marker has value None and is ignored
        elif a == "light":
            self._actions.light(event.value)
        elif a == "send_key":
            self._actions.send_key(event.key)
        elif a in ("music_play", "music_pause", "music_prev", "music_next"):
            self._actions.music(a)

    def tick(self) -> None:
        try:
            self._actions.tick()
        except Exception:
            log.exception("error in tick")
        try:
            self._replies.tick()
        except Exception:
            log.exception("error in reply tick")

    def close(self) -> None:
        """Stop background workers. Never raises."""
        try:
            self._replies.close()
        except Exception:
            log.exception("error closing the reply layer")


def pump(transport: Any, dispatcher: Dispatcher, should_stop: Callable[[], bool]) -> None:
    """Read events from the transport and dispatch them until should_stop()."""
    while not should_stop():
        for event in transport.read_events():
            dispatcher.handle(event)
        dispatcher.tick()
