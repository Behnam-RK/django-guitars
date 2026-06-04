from collections import defaultdict

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


class DisableSignals:
    """Context manager that temporarily disconnects Django signals.

    Stashes all receivers for the given signals on enter and reconnects
    them on exit. Used by ``UpdatableModel.update(_disable_signals=True)``
    to suppress ``pre_save``/``post_save`` during silent updates.

    Usage::

        with DisableSignals():
            instance.save()  # no signals fire

        with DisableSignals(signals=[post_save]):
            instance.save()  # only post_save is suppressed
    """

    DEFAULT_SIGNALS = [
        pre_init,
        post_init,
        pre_save,
        post_save,
        pre_delete,
        post_delete,
        pre_migrate,
        post_migrate,
    ]

    def __init__(self, signals: list[Signal] | None = None):
        self.stashed_signals = defaultdict(list)
        self.disabled_signals = signals or self.DEFAULT_SIGNALS

    def __enter__(self):
        for signal in self.disabled_signals:
            self.disconnect(signal)

    def __exit__(self, exc_type, exc_val, exc_tb):
        for signal in list(self.stashed_signals.keys()):
            self.reconnect(signal)

    def disconnect(self, signal):
        self.stashed_signals[signal] = signal.receivers
        signal.receivers = []

    def reconnect(self, signal):
        signal.receivers = self.stashed_signals.get(signal, [])
        signal.sender_receivers_cache.clear()
        del self.stashed_signals[signal]
