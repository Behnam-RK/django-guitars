"""#66 (a): ``Part`` deleted, then recreated on the same table, generating at every step."""

from django.db import models

from guitars.models import SetarModel


class Maker(SetarModel):
    name = models.CharField(max_length=20, default='o')


class Part(SetarModel):
    owner = models.ForeignKey(Maker, on_delete=models.CASCADE, related_name='parts')
