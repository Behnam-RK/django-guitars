"""The owner half of the retirement-ordering pair (issue #49). Its table is the one the cascade
rule fires on, so ``_table_app_labels`` hosts the retirement here -- while the create was written
into the *other* app, which is the whole shape."""

from guitars.models import SoftDeletableModel


class Retiree(SoftDeletableModel):
    """``SoftDeletableModel``, not ``SetarModel``: ``_updated_at`` would call for the shared
    trigger function, and a second app creating it fails ``migrate`` on a database that already
    has testapp's. Only ``_deleted_at`` matters to a cascade rule."""

    class Meta:
        app_label = 'crossapp_retire_owner'
