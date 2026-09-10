"""The related half. Its key was ``CASCADE`` when 0002 wrote the rule and is ``SET_NULL`` now,
so the models no longer call for it and the generator retires it -- into the *owner's* app, with
nothing ordering that drop after this app's create until 2.10.0."""

from django.db import models

from guitars.models import SoftDeletableModel


class Dependant(SoftDeletableModel):
    owner = models.ForeignKey(
        'crossapp_retire_owner.Retiree', on_delete=models.SET_NULL, null=True, related_name='kids'
    )

    class Meta:
        app_label = 'crossapp_retire_child'
