"""A third app owning the same dependent, so every one of the three rules reads arms in the
other two. The shape that could close a cycle if edges pointed at app leaves rather than at
the migrations that create what a rule names."""

from django.db.models import DO_NOTHING

from guitars.models import OwningForeignKey, SetarModel


class ThirdOwner(SetarModel):
    target = OwningForeignKey(
        'crossapp_dependent.Shared', on_delete=DO_NOTHING, null=True, related_name='+'
    )

    class Meta:
        app_label = 'crossapp_third'
