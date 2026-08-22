"""The tenanted root of an MTI chain that crosses an app boundary. Its tenant column is what a
descendant's policy reads through the owner join -- and it arrives in a migration *later* than
the table, which is the ordering the edge exists for. Not in ``LOCAL_APPS``."""

from django.db.models import CASCADE, CharField, ForeignKey

from guitars.models import LiveManager, SetarModel
from guitars.tenancy import tenanted_manager


class LocalLabel(SetarModel):
    """The tenant this app scopes by. Local rather than ``testapp.Label`` on purpose: a
    ``CASCADE`` key into another app puts a cascade rule in *that* app's migration, so
    pointing at the project tenant would move what an unrelated app's ``--check`` expects."""

    name = CharField(max_length=100)

    class Meta:
        app_label = 'crossapp_tenant_ancestor'


class TenantedAncestor(SetarModel):
    """Tenanted by hand rather than through ``GuitarModel``, whose FK is fixed to
    ``GUITARS_TENANT_MODEL``. The dimension name is what matters: the policy predicates on
    ``tenant.label``, the same GUC the project setting names."""

    name = CharField(max_length=100)
    label = ForeignKey(LocalLabel, on_delete=CASCADE, related_name='ancestors')

    objects = tenanted_manager(_manager_class=LiveManager, label='label')

    class Meta:
        app_label = 'crossapp_tenant_ancestor'
