from __future__ import annotations

import threading
from typing import TYPE_CHECKING, TypedDict

from django.db.models.signals import (
    post_delete,
    post_init,
    post_migrate,
    post_save,
    pre_delete,
    pre_init,
    pre_migrate,
    pre_save,
)
from django.dispatch import Signal


if TYPE_CHECKING:
    from types import TracebackType


class _Stash(TypedDict):
    count: int
    original: list


# Process-global, keyed by signal, so overlapping DisableSignals() instances -- on
# different threads, or nested on the same one -- agree on one true stash per signal
# instead of each keeping its own. `signal.receivers` is itself process-global mutable
# state with no lock of its own; without this, a second block's __exit__ would restore
# from a stash it took *after* the first block had already emptied the list, overwriting
# the first block's correct restore with an empty one and permanently losing every
# receiver. `_lock` serialises every read/write of both this dict and `signal.receivers`
# so "stash it" and "count the stash" never interleave across threads.
_lock = threading.Lock()
_state: dict[Signal, _Stash] = {}


class DisableSignals:
    """Context manager that temporarily disconnects Django signals.

    Stashes all receivers for the given signals on enter and reconnects
    them on exit. Used by ``UpdatableModel.update(_disable_signals=True)``
    to suppress ``pre_save``/``post_save`` during silent updates.

    Thread-safe and re-entrant: overlapping blocks (nested on one thread, or concurrent
    across threads) for the same signal share one reference-counted stash, taken by
    whichever block enters first and restored only when the last one exits.

    Usage::

        with DisableSignals() as ds:
            instance.save()  # no signals fire

        with DisableSignals(signals=[post_save]):
            instance.save()  # only post_save is suppressed
    """

    DEFAULT_SIGNALS: list[Signal] = [
        pre_init,
        post_init,
        pre_save,
        post_save,
        pre_delete,
        post_delete,
        pre_migrate,
        post_migrate,
    ]

    def __init__(self, signals: list[Signal] | None = None) -> None:
        self.disabled_signals: list[Signal] = signals or self.DEFAULT_SIGNALS

    def __enter__(self) -> DisableSignals:
        for signal in self.disabled_signals:
            self.disconnect(signal)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for signal in self.disabled_signals:
            self.reconnect(signal)

    def disconnect(self, signal: Signal) -> None:
        with _lock:
            entry = _state.setdefault(signal, {'count': 0, 'original': signal.receivers})
            if entry['count'] == 0:
                entry['original'] = signal.receivers
                signal.receivers = []
            entry['count'] += 1

    def reconnect(self, signal: Signal) -> None:
        with _lock:
            entry = _state[signal]
            entry['count'] -= 1
            if entry['count'] == 0:
                signal.receivers = entry['original']
                signal.sender_receivers_cache.clear()
                del _state[signal]
