"""#66 (c): ``Hub``'s children, and ``Hub`` itself while it sits here."""

from django.db import models

from guitars.models import OwningForeignKey, SetarModel


class Spoke(SetarModel):
    hub = models.ForeignKey('issue66_anc.Hub', on_delete=models.CASCADE, related_name='spokes')


class Keeper(SetarModel):
    hub = OwningForeignKey(
        'issue66_anc.Hub', on_delete=models.DO_NOTHING, null=True, related_name='keepers'
    )
