"""The ancestor half of the retirement-ordering pair (issue #49). It declares ``_deleted_at``,
so every cascade rule aimed at a descendant fires on *this* table -- and the retirement is
hosted here, while the create is written into the app that walked the descendant."""

from guitars.models import SoftDeletableModel


class Retiree(SoftDeletableModel):
    """``SoftDeletableModel``, not ``SetarModel``: ``_updated_at`` would call for the shared
    trigger function, and a second app creating it fails ``migrate`` on a database that already
    has testapp's. Only ``_deleted_at`` matters to a cascade rule."""

    class Meta:
        app_label = 'crossapp_retire_owner'
