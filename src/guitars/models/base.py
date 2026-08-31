"""The instrument ladder: abstract bases named by string count, one rung per capability
the database enforces. See README.md's table for the four rungs and CLAUDE.md for the
1.0.0 renumbering; each capability is also a standalone mixin below."""

import logging
from contextlib import nullcontext

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import transaction
from django.db.models import CASCADE, DateTimeField, ForeignKey
from django.db.models.base import Model
from django.db.models.functions import Now
from django.utils.functional import cached_property

from guitars.checks import register_checks as register_shape_checks
from guitars.tenancy import tenanted_manager
from guitars.tenancy.checks import register_checks

from .soft_deletion import AllObjectsManager, ArchiveManager, LiveManager, SoftDeletableModel


logger = logging.getLogger('guitars.models')


class DatedModel(Model):
    """Auto-managed ``_created_at`` / ``_updated_at``, both ``NOW()``-defaulted and kept
    current by a PostgreSQL statement trigger (not Django signals) -- accurate under bulk
    operations and raw SQL that bypass ``.save()``."""

    _created_at = DateTimeField(verbose_name='Created at', db_default=Now(), editable=False)
    _updated_at = DateTimeField(verbose_name='Updated at', db_default=Now(), editable=False)

    class Meta:
        abstract = True


class UpdatableModel(Model):
    """Adds ``.update(**attrs)``: set attributes and save in one call, with fine-grained
    field control -- see :meth:`update` and ``docs/api-reference.md``."""

    class Meta:
        abstract = True

    @cached_property
    def _updatable_fields(self) -> set[str]:
        fields = set()
        for field in self._meta._get_fields(reverse=False, include_hidden=True):
            fields.add(field.name)
            column = getattr(field, 'column', None)
            if column:  # To accept foreign keys in fk_id format! (relations have no column)
                fields.add(column)

        return fields

    def _prepare_update(
        self,
        _save: bool,
        _save_all_fields: bool,
        _raise_for_excessive: bool,
        attrs: dict[str, object],
    ) -> tuple[dict[str, object], set[str] | None]:
        """Validate attrs and set non-M2M fields in memory. Returns ``(m2m_attrs,
        update_fields)`` for the save step; ``update_fields=None`` means save every field."""
        fields = self._updatable_fields
        m2m_fields = {field.name for field in self._meta.local_many_to_many}
        given_fields = set(attrs.keys())
        excessive_fields = given_fields - fields
        updating_fields = (fields & given_fields) - m2m_fields
        m2m_attrs = {attr: value for attr, value in attrs.items() if attr in m2m_fields}

        if excessive_fields:
            if _raise_for_excessive:
                raise ValueError(
                    f'Invalid arguments: {excessive_fields}. (valid choices: {fields})'
                )
            # _raise_for_excessive=False means "ignore and proceed", not "ignore silently"
            # -- a typo'd kwarg previously vanished with zero signal. DEBUG rather than
            # WARNING: this is the documented, requested behaviour, not a surprise.
            logger.debug(
                '%s.update() ignored unknown field(s) %s (valid choices: %s)',
                type(self).__name__,
                sorted(excessive_fields),
                sorted(fields),
            )

        if not _save:
            if m2m_attrs:
                raise ValueError('Cannot update m2m fields without saving the instance!')
            if _save_all_fields:
                # _save_all_fields says "write every field" but _save=False means nothing is
                # written this call, so the combination has no meaning -- it previously
                # computed update_fields=None and never used it (self.save() is unreachable).
                raise ValueError(
                    '_save_all_fields=True has no effect when _save=False -- nothing is '
                    'saved this call. Drop _save_all_fields, or pass _save=True (the '
                    'default) if you meant to write every field.'
                )

        for attr, attr_value in attrs.items():
            if attr in updating_fields:
                setattr(self, attr, attr_value)

        update_fields = None if _save_all_fields else updating_fields
        return m2m_attrs, update_fields

    def update(
        self,
        _save: bool = True,
        _save_all_fields: bool = False,
        _raise_for_excessive: bool = True,
        _disable_signals: bool = False,
        **attrs: object,
    ) -> None:
        """Set attributes and optionally persist, writing only changed fields unless
        ``_save_all_fields=True``. Full parameter semantics in
        ``docs/api-reference.md``."""
        m2m_attrs, update_fields = self._prepare_update(
            _save, _save_all_fields, _raise_for_excessive, attrs
        )

        if _save:
            from django.db.models.signals import post_save, pre_save

            from guitars.signals import DisableSignals
            from guitars.tenancy.reporting import report_once
            from guitars.tenancy.spec import tenant_spec

            signals_context = (
                DisableSignals(signals=[pre_save, post_save])
                if _disable_signals
                else nullcontext()
            )
            # update_fields=set() (empty, not None) makes self.save() below a no-op --
            # Django issues no SQL and fires no signals at all -- so there is nothing for
            # _disable_signals to have bypassed. Only report when the save will actually run.
            save_will_write = update_fields is None or update_fields
            if _disable_signals and save_will_write and tenant_spec(type(self)):
                report_once(
                    (type(self), 'update_disable_signals_bypasses_tenant_guard'),
                    f'{type(self).__name__}.update(_disable_signals=True) suppresses '
                    'pre_save, which disables the tenant write guard for this save.',
                    model=type(self).__name__,
                )
            with signals_context:
                with transaction.atomic():
                    self.save(update_fields=update_fields)
                    for attr, attr_value in m2m_attrs.items():
                        getattr(self, attr).set(attr_value, clear=True)

    async def aupdate(
        self,
        _save: bool = True,
        _save_all_fields: bool = False,
        _raise_for_excessive: bool = True,
        _disable_signals: bool = False,
        **attrs: object,
    ) -> None:
        """A thread hop onto ``.update()``; the shared thread pool gives concurrent calls no
        same-thread guarantee. Safe: ``DisableSignals`` is process-global, lock-protected, and
        reference-counted, so overlapping ``_disable_signals=True`` blocks nest, not clobber."""
        await _update_async(
            self,
            _save=_save,
            _save_all_fields=_save_all_fields,
            _raise_for_excessive=_raise_for_excessive,
            _disable_signals=_disable_signals,
            **attrs,
        )


#: Built once, not reconstructed on every ``aupdate()`` call -- see that method's
#: docstring. ``UpdatableModel.update`` is the unbound function (its own first
#: parameter is ``self``), so this is called as ``await _update_async(instance, ...)``.
_update_async = sync_to_async(UpdatableModel.update)


class HasCachedPropertyModel(Model):
    """Overrides ``refresh_from_db()`` to clear all cached ``@cached_property`` values,
    preventing stale in-memory reads after a database refresh."""

    class Meta:
        abstract = True

    def expire_cached_properties(self, *properties):
        # The whole MRO, not just this class's own __dict__: a cached_property declared on
        # a mixin lives in *that* class's __dict__, while its cached value lands in
        # self.__dict__ -- reading only the leaf class silently skipped inherited ones.
        for klass in type(self).__mro__:
            for key, value in vars(klass).items():
                if properties and key not in properties:
                    continue
                if isinstance(value, cached_property):
                    self.__dict__.pop(key, None)

    def refresh_from_db(self, *args, **kwargs):
        self.expire_cached_properties()
        return super().refresh_from_db(*args, **kwargs)


class TarModel(UpdatableModel, HasCachedPropertyModel):
    """The root: ``.update()`` (``UpdatableModel``) plus cached-property invalidation
    (``HasCachedPropertyModel``), no columns and no database behaviour. The introspection
    helpers (``app_label()`` etc.) stay one rung up, on ``DutarModel``, as they always were."""

    class Meta:
        abstract = True


class DutarModel(DatedModel, TarModel):
    """``TarModel`` plus database-managed timestamps (``DatedModel``): ``_created_at`` /
    ``_updated_at``, the latter ridden by a statement-level trigger so it stays honest
    under ``bulk_update`` and raw SQL."""

    class Meta:
        abstract = True

    @classmethod
    def app_label(cls) -> str:
        """Returns app label in lowercase."""
        if not (hasattr(cls, '_meta') and cls._meta.app_label):
            raise AttributeError(f'{cls.__name__}._meta.app_label is not set!')

        return cls._meta.app_label

    @classmethod
    def model_name(cls) -> str:
        """Returns model name in lowercase"""
        if not (hasattr(cls, '_meta') and cls._meta.model_name):
            raise AttributeError(f'{cls.__name__}._meta.model_name is not set!')

        return cls._meta.model_name

    @classmethod
    def class_name(cls) -> str:
        """Returns class name"""
        return cls.__name__

    def __repr__(self) -> str:
        # Only concrete non-M2M fields — M2M / reverse relations on an unsaved
        # instance raise ValueError formatted with the instance itself, which
        # would recurse back into __repr__.
        representation = f'<{self.__class__.__name__} ID:{self.pk} - '
        for field in self._meta.fields:
            if not field.editable:
                continue
            value = getattr(self, field.name)
            if value is None:
                continue
            representation += f'{field.name}: {value} - '
        return representation + '>'


class SetarModel(DutarModel, SoftDeletableModel):
    """``DutarModel`` plus PostgreSQL-enforced soft deletion: ``_deleted_at`` and the
    three managers. The default rung for a model that isn't tenanted -- reach for
    ``GuitarModel`` only when rows belong to a tenant."""

    class Meta(SoftDeletableModel.Meta):
        abstract = True


# Both read once, at import, and both private: a consumer importing them would hold a
# snapshot `override_settings` cannot move. Ask `guitars.tenancy.tenant_spec(Model)`
# instead, derived per model and always current.
_TENANT_MODEL = getattr(settings, 'GUITARS_TENANT_MODEL', None)
_TENANT_FIELD = getattr(settings, 'GUITARS_TENANT_FIELD', 'tenant')


class GuitarModel(SetarModel):
    """The full kit: ``SetarModel`` plus tenancy -- see ``docs/tenancy.md``. Inert without
    ``GUITARS_TENANT_MODEL`` (caught by the ``E003`` check, not an import-time crash); its
    CASCADE tenant FK scales cascading soft-delete rules with tenanted models, not rows."""

    #: Whether the tenant FK and the scoped managers were actually installed below.
    #: Read by the system check, which cannot import this class at module scope (the
    #: tenancy package is imported *by* this module) and must not re-derive the condition.
    _guitars_tenancy_installed = False

    class Meta(SetarModel.Meta):
        abstract = True

        # base_manager_name is deliberately left unset -- Django's own rule is that a base
        # manager must not filter rows, and it sits on the save() path where the deny-list
        # a missing scope raises from can't reach it. See ADR-0004 for the full evidence.


def _install_tenancy(tenant_model: str, field_name: str) -> None:
    """Contribute the tenant field and scoped managers to ``GuitarModel`` after the class
    body (the field's name comes from a setting, which a class body can't bind) via
    ``add_to_class`` -- copied down to every concrete subclass the same as a declared one."""
    GuitarModel.add_to_class(
        field_name,
        ForeignKey(
            tenant_model,
            on_delete=CASCADE,
            # CASCADE, not PROTECT: a tenant's rows are meaningless without it, and the
            # database-level soft delete means "cascade" archives rather than destroys.
            related_name='%(app_label)s_%(class)s_set',
            # Templated, because every tenanted model in every app would otherwise collide
            # on the same reverse accessor.
            editable=False,
            # Framework-owned: kept out of ModelForms and the admin, and filled from the
            # active scope by the write guard.
        ),
    )
    for name, manager_class in (
        ('objects', LiveManager),
        ('_archives', ArchiveManager),
        ('_all_objects', AllObjectsManager),
    ):
        # All three, not just the default: ``_archives`` and ``_all_objects`` exist to see
        # rows ``objects`` hides, and an unscoped one would see every tenant's.
        GuitarModel.add_to_class(
            name,
            tenanted_manager(
                _manager_class=manager_class,
                autofill=True,
                **{field_name: field_name},
            ),
        )
    GuitarModel._guitars_tenancy_installed = True


# Registered unconditionally, not behind tenancy.install(): install() never fires for a
# pure-library project with GUITARS_TENANT_MODEL unset -- exactly the case E003 exists to
# catch. Checks are cheap; the enforcement machinery isn't, so that stays behind install().
register_checks()
register_shape_checks()

if _TENANT_MODEL:
    _install_tenancy(_TENANT_MODEL, _TENANT_FIELD)
