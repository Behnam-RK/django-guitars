"""The owning half of the cross-app ownership pair. Its rule's last-owner guard reads a
co-owner column in ``crossapp_dependent``, and its own table is read by that app's rule --
so an edge is needed in *both* directions, which is the shape the consumer hit."""

from django.db.models import DO_NOTHING

from guitars.models import OwningForeignKey, SetarModel


class Owner(SetarModel):
    """``DO_NOTHING`` as ``docs/owned-relations.md`` tells a consumer to use -- ``SET_NULL``
    has Django's ``Collector`` clear the key before the rule rewrites the ``DELETE``."""

    target = OwningForeignKey(
        'crossapp_dependent.Shared', on_delete=DO_NOTHING, null=True, related_name='+'
    )

    class Meta:
        app_label = 'crossapp_owner'
