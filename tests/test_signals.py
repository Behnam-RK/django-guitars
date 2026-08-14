"""Tests for guitars.signals.DisableSignals."""

import threading

import pytest
from django.db.models.signals import post_save, pre_save

from guitars.signals import DisableSignals
from tests.testapp.models import Band


@pytest.mark.django_db
def test_disable_signals_suppresses_then_restores():
    received = []

    def receiver(sender, instance, **kwargs):
        received.append(instance.name)

    post_save.connect(receiver, sender=Band, weak=False)
    try:
        with DisableSignals():
            Band.objects.create(name='muted')
        assert received == []  # suppressed inside the context

        Band.objects.create(name='heard')
        assert received == ['heard']  # receiver reconnected on exit
    finally:
        post_save.disconnect(receiver, sender=Band)


@pytest.mark.django_db
def test_disable_signals_only_disconnects_listed_signal():
    pre_calls, post_calls = [], []

    def on_pre(sender, **kwargs):
        pre_calls.append(1)

    def on_post(sender, **kwargs):
        post_calls.append(1)

    pre_save.connect(on_pre, sender=Band, weak=False)
    post_save.connect(on_post, sender=Band, weak=False)
    try:
        with DisableSignals(signals=[post_save]):
            Band.objects.create(name='x')

        assert pre_calls == [1]  # pre_save left connected
        assert post_calls == []  # only post_save suppressed
    finally:
        pre_save.disconnect(on_pre, sender=Band)
        post_save.disconnect(on_post, sender=Band)


def test_disable_signals_concurrent_use_does_not_lose_receivers():
    """Two overlapping ``DisableSignals()`` blocks must not permanently wipe receivers:
    ``signal.receivers`` is process-global with no lock, so B stashing A's now-emptied
    list then restoring it on exit wipes every receiver. Forced via events, not luck."""

    def on_save(sender, **kwargs):
        pass

    pre_save.connect(on_save, sender=Band, weak=False)
    try:
        original = list(pre_save.receivers)
        assert original  # sanity: something is actually connected to begin with

        a_entered = threading.Event()
        b_entered = threading.Event()
        a_exited = threading.Event()

        def thread_a():
            with DisableSignals():
                a_entered.set()
                b_entered.wait(timeout=5)
            a_exited.set()

        def thread_b():
            a_entered.wait(timeout=5)
            with DisableSignals():
                b_entered.set()
                a_exited.wait(timeout=5)

        ta = threading.Thread(target=thread_a)
        tb = threading.Thread(target=thread_b)
        ta.start()
        tb.start()
        ta.join(timeout=5)
        tb.join(timeout=5)

        assert not ta.is_alive()
        assert not tb.is_alive()
        assert pre_save.receivers == original
    finally:
        pre_save.disconnect(on_save, sender=Band)
