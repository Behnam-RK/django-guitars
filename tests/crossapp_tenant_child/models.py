"""An MTI child of a tenanted model in *another* app. Its policy has no tenant column of its
own, so the predicate is an owner join naming the ancestor's table and its tenant column --
both resolved when ``CREATE POLICY`` is parsed. Not in ``LOCAL_APPS``."""

from django.db.models import IntegerField

from tests.crossapp_tenant_ancestor.models import TenantedAncestor


class TenantedChild(TenantedAncestor):
    seats = IntegerField(default=0)

    class Meta:
        app_label = 'crossapp_tenant_child'
