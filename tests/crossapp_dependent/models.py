"""The dependent half of the cross-app ownership pair: a model owned from *another* app, so
each owner's rule names a table its own app's migrations never create. Not in
``INSTALLED_APPS`` -- its tests install it, as ``schema_qualified``'s do."""

from django.db.models import DO_NOTHING

from guitars.models import OwningForeignKey, SetarModel


class Shared(SetarModel):
    """Owned from both ``crossapp_owner`` and this app. Its table is what the other app's
    rule updates, and what that app's migration must be ordered against."""

    class Meta:
        app_label = 'crossapp_dependent'


class LocalOwner(SetarModel):
    """The co-owner living *with* the dependent. Its column is what ``crossapp_owner``'s rule
    reads as a sibling arm, and the reason that rule needs an edge into this app."""

    target = OwningForeignKey(Shared, on_delete=DO_NOTHING, null=True, related_name='+')

    class Meta:
        app_label = 'crossapp_dependent'
