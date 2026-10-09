"""#66 (b): ``Crew`` renamed to ``Squad``, and a new ``Crew`` retakes ``Crew``'s old table."""

from django.db import models

from guitars.models import SetarModel


class Boss(SetarModel):
    name = models.CharField(max_length=20, default='o')


class Squad(SetarModel):
    owner = models.ForeignKey(Boss, on_delete=models.CASCADE, related_name='squads')


class Crew(SetarModel):
    lead = models.ForeignKey(Boss, on_delete=models.CASCADE, related_name='crews')
