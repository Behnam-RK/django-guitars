"""#66 (c): ``Hub`` moves to ``issue66_shop`` and back; its children stay in that app."""

from django.db import models

from guitars.models import SetarModel


class Hub(SetarModel):
    name = models.CharField(max_length=20, default='h')

    class Meta(SetarModel.Meta):
        db_table = 'issue66_anc_hub'
