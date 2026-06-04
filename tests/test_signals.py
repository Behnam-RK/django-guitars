"""Tests for guitars.signals.DisableSignals."""

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
