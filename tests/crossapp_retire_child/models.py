"""The other half: an MTI child one app down, and a dependant pointing at it. Plain supported
Django -- no proxy, no shared ``db_table`` -- and enough to split the pair, because the create
is emitted while walking ``Heir`` here while the rule fires on the ancestor's table."""

from django.db import models

from guitars.models import SoftDeletableModel
from tests.crossapp_retire_owner.models import Retiree


class Heir(Retiree):
    class Meta:
        app_label = 'crossapp_retire_child'


class Dependant(SoftDeletableModel):
    """Its key was ``CASCADE`` when the rule was written and is ``SET_NULL`` now, so the models
    no longer call for it and the generator retires it -- into the *ancestor's* app."""

    heir = models.ForeignKey(Heir, on_delete=models.SET_NULL, null=True, related_name='kids')

    class Meta:
        app_label = 'crossapp_retire_child'
