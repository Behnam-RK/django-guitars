"""Tests for the instrument ladder, and for what ``GuitarModel`` contributes.

``GuitarModel``'s tenancy wiring runs at **import time**, under ``GUITARS_TENANT_MODEL``
and ``GUITARS_TENANT_FIELD``: the foreign key's name comes from a setting, so it cannot be
declared in a class body and is contributed with ``add_to_class`` after it. That puts the
behaviour out of reach of ``override_settings``, which changes a setting long after the
module has been imported and the field named.

So these run real Django setups in subprocesses, one per configuration. Not a workaround
-- the thing under test *is* import-time, setting-dependent module state, and a subprocess
is the only instrument that can vary it. Each probe declares its models into the
``guitars`` app label so no throwaway package is needed on ``sys.path``.

The rung shift itself (``TarModel`` / ``DutarModel`` / ``SetarModel``) needs none of this:
those are plain classes, asserted directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

from guitars.models import (
    DatedModel,
    DutarModel,
    GuitarModel,
    HasCachedPropertyModel,
    SetarModel,
    SoftDeletableModel,
    TarModel,
    UpdatableModel,
)


# ─────────────────────────── the rungs themselves ─────────────────────────── #


class TestTheLadder:
    """Each rung is the one below it plus exactly one capability."""

    def test_every_rung_is_abstract(self):
        for rung in (TarModel, DutarModel, SetarModel, GuitarModel):
            assert rung._meta.abstract, rung.__name__

    def test_tar_is_the_helpers_and_no_columns(self):
        assert issubclass(TarModel, UpdatableModel)
        assert issubclass(TarModel, HasCachedPropertyModel)

        # The point of the rung: it costs a consumer no schema at all.
        assert TarModel._meta.local_fields == []

    def test_dutar_adds_timestamps_to_tar(self):
        assert issubclass(DutarModel, TarModel)
        assert issubclass(DutarModel, DatedModel)

        assert {field.name for field in DutarModel._meta.local_fields} == {
            '_created_at',
            '_updated_at',
        }

    def test_setar_adds_soft_deletion_to_dutar(self):
        assert issubclass(SetarModel, DutarModel)
        assert issubclass(SetarModel, SoftDeletableModel)

        assert '_deleted_at' in {field.name for field in SetarModel._meta.local_fields}
        assert {'objects', '_archives', '_all_objects'} <= {
            manager.name for manager in SetarModel._meta.managers
        }

    def test_guitar_is_setar_plus_tenancy(self):
        assert issubclass(GuitarModel, SetarModel)

        # Tenancy is the *only* thing it adds, and it adds it from settings -- so with the
        # setting unset (this suite's configuration) the rung is inert by construction.
        # That state is what `guitars.tenancy.E003` exists to catch; see TestTheSystemCheck.
        assert GuitarModel._guitars_tenancy_installed is False

    def test_the_soft_delete_index_is_inherited_by_the_tenanted_rung(self):
        """``Meta`` inheritance is easy to break by re-declaring ``class Meta``."""
        names = [index.name for index in GuitarModel._meta.indexes]

        assert names == ['%(class)s_deleted_at']
        assert GuitarModel._meta.default_manager_name == 'objects'

    def test_the_introspection_helpers_sit_on_dutar(self):
        """Where the rung shift left them -- a pure rename, no redistribution."""
        for name in ('app_label', 'model_name', 'class_name'):
            assert hasattr(DutarModel, name)
            assert not hasattr(TarModel, name)


# ──────────────────────── import-time tenancy wiring ──────────────────────── #

#: Configures Django from scratch, declares a tenant model and a tenanted model into the
#: ``guitars`` app label, then prints whatever the probe body put in ``result``.
_PROBE = """
import json

import django
from django.conf import settings

settings.configure(
    INSTALLED_APPS=['guitars'],
    DATABASES={},
    DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
    LOCAL_APPS=['guitars'],
    USE_TZ=True,
    **__SETTINGS_EXTRA__,
)
django.setup()

from django.db.models import CharField, Model

from guitars.models import GuitarModel


class Tenant(Model):
    name = CharField(max_length=50)

    class Meta:
        app_label = 'guitars'


class Thing(GuitarModel):
    title = CharField(max_length=50)

    # abstract must be re-declared: inheriting a Meta whose abstract is True would make
    # this abstract too, which is Django's oldest MTI footgun.
    class Meta(GuitarModel.Meta):
        app_label = 'guitars'
        abstract = False


result = {}
__BODY__
print('---RESULT---')
print(json.dumps(result))
"""


def _probe(body: str, **settings_extra: object) -> dict:
    """Run *body* inside a fresh Django process configured with *settings_extra*."""
    # Token substitution rather than str.format: the template is Python source full of
    # literal braces, and escaping every one of them is a trap the next edit falls into.
    script = _PROBE.replace('__SETTINGS_EXTRA__', repr(settings_extra)).replace(
        '__BODY__', textwrap.dedent(body).strip()
    )
    completed = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or '---RESULT---' not in completed.stdout:
        pytest.fail(
            f'probe failed (exit {completed.returncode})\n'
            f'--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}'
        )
    return json.loads(completed.stdout.split('---RESULT---', 1)[1])


#: The default field name, and a renamed one. Both are exercised everywhere below, because
#: GUITARS_TENANT_FIELD is not cosmetic: it names the column, the reverse accessor *and*
#: the scope dimension, and a rename that only reached two of the three would leave
#: `tenant(shop=s)` scoping a dimension no manager filters on.
FIELD_NAMES = [
    pytest.param({}, 'tenant', id='default-name'),
    pytest.param({'GUITARS_TENANT_FIELD': 'shop'}, 'shop', id='renamed'),
]


@pytest.mark.parametrize(('extra', 'field_name'), FIELD_NAMES)
class TestWhatGuitarModelContributes:
    """With ``GUITARS_TENANT_MODEL`` set, a concrete subclass gets the whole kit."""

    def test_the_tenant_foreign_key(self, extra, field_name):
        result = _probe(
            f"""
            field = Thing._meta.get_field({field_name!r})
            result['target'] = field.related_model._meta.label
            result['on_delete'] = field.remote_field.on_delete.__name__
            result['null'] = field.null
            result['editable'] = field.editable
            result['column'] = field.column
            result['reverse'] = field.remote_field.get_accessor_name()
            """,
            GUITARS_TENANT_MODEL='guitars.Tenant',
            **extra,
        )

        assert result['target'] == 'guitars.Tenant'
        assert result['on_delete'] == 'CASCADE'
        assert result['null'] is False
        assert result['editable'] is False
        assert result['column'] == f'{field_name}_id'
        # Templated per app and model, or every tenanted model in a project would collide
        # on the same reverse accessor.
        assert result['reverse'] == 'guitars_thing_set'

    def test_all_three_managers_are_scoped_on_that_dimension(self, extra, field_name):
        result = _probe(
            """
            result['managers'] = {
                manager.name: [
                    getattr(manager, '_tenant_dimensions', None),
                    getattr(manager, '_tenant_autofill', None),
                ]
                for manager in Thing._meta.managers
            }
            """,
            GUITARS_TENANT_MODEL='guitars.Tenant',
            **extra,
        )

        # _archives and _all_objects exist to see rows `objects` hides; an unscoped one
        # would see every tenant's, which is the leak this feature is about.
        for name in ('objects', '_archives', '_all_objects'):
            dimensions, autofill = result['managers'][name]
            assert dimensions == {field_name: field_name}, name
            # autofill=True is passed explicitly, overriding GUITARS_TENANT_AUTOFILL: the
            # field is framework-owned and editable=False, so naming it at every call site
            # would be ceremony.
            assert autofill is True, name

    def test_the_underlying_managers_survive_the_wrapping(self, extra, field_name):
        """Soft deletion must still work: each manager keeps its own queryset filter."""
        result = _probe(
            """
            result['bases'] = {
                manager.name: [base.__name__ for base in type(manager).__mro__]
                for manager in Thing._meta.managers
            }
            """,
            GUITARS_TENANT_MODEL='guitars.Tenant',
            **extra,
        )

        assert 'LiveManager' in result['bases']['objects']
        assert 'ArchiveManager' in result['bases']['_archives']
        assert 'AllObjectsManager' in result['bases']['_all_objects']

    def test_the_base_manager_stays_unscoped(self, extra, field_name):
        """Deliberate, and the reasoning is in ``GuitarModel.Meta``.

        ``_base_manager`` is on the ``save()`` path and Django's rule is that it must not
        filter; scoping it would enforce partially (writes through, reads denied) and the
        row-level-security policy already covers it completely.
        """
        result = _probe(
            """
            result['name'] = Thing._meta.base_manager.name
            result['dimensions'] = getattr(
                Thing._meta.base_manager, '_tenant_dimensions', None
            )
            """,
            GUITARS_TENANT_MODEL='guitars.Tenant',
            **extra,
        )

        # Django's auto-created plain Manager names itself this.
        assert result['name'] == '_base_manager'
        assert result['dimensions'] is None

    def test_discovery_will_ask_for_a_policy_on_that_column(self, extra, field_name):
        """The end of the chain: setting -> field -> dimension -> policy predicate.

        Without this the three links above could each be right while the generator still
        emitted no policy, or one predicating on the wrong column.
        """
        result = _probe(
            """
            from django.apps import apps

            from guitars.tenancy.discovery import app_coverage

            coverage = app_coverage(apps.get_app_config('guitars'))
            result['tables'] = {
                table: table_coverage.columns for table, table_coverage in coverage.tables.items()
            }
            result['notes'] = coverage.notes
            """,
            GUITARS_TENANT_MODEL='guitars.Tenant',
            **extra,
        )

        assert result['tables'] == {'guitars_thing': {field_name: f'{field_name}_id'}}
        assert result['notes'] == []


class TestTheSystemCheck:
    """``guitars.tenancy.E003``: a concrete GuitarModel with no tenant model configured."""

    def test_it_errors_when_the_setting_is_missing(self):
        result = _probe(
            """
            from django.core.checks import run_checks

            errors = run_checks()
            result['ids'] = sorted(error.id for error in errors)
            result['messages'] = [error.msg for error in errors]
            result['hints'] = [error.hint for error in errors]
            """
        )

        assert result['ids'] == ['guitars.tenancy.E003']
        # It has to name the offender -- a project with fifty models needs to know which.
        assert 'guitars.Thing' in result['messages'][0]
        # And the way out, both ways: configure tenancy, or drop to the untenanted rung.
        assert 'GUITARS_TENANT_MODEL' in result['hints'][0]
        assert 'SetarModel' in result['hints'][0]

    def test_it_is_silent_once_the_setting_is_there(self):
        result = _probe(
            """
            from django.core.checks import run_checks

            result['ids'] = sorted(error.id for error in run_checks())
            """,
            GUITARS_TENANT_MODEL='guitars.Tenant',
        )

        assert result['ids'] == []

    def test_it_is_silent_for_a_project_that_never_used_the_rung(self):
        """A project on the lower rungs must be able to run ``manage.py check`` clean.

        Keyed on concrete subclasses, not on the setting alone -- otherwise every
        untenanted project using guitars would be nagged about a feature it declined.
        """
        completed = subprocess.run(
            [
                sys.executable,
                '-c',
                textwrap.dedent("""
                    import django
                    from django.conf import settings

                    settings.configure(
                        INSTALLED_APPS=['guitars'],
                        DATABASES={},
                        DEFAULT_AUTO_FIELD='django.db.models.BigAutoField',
                        LOCAL_APPS=['guitars'],
                        USE_TZ=True,
                    )
                    django.setup()

                    from django.core.checks import run_checks
                    from django.db.models import CharField

                    from guitars.models import SetarModel

                    class Plain(SetarModel):
                        title = CharField(max_length=50)

                        class Meta(SetarModel.Meta):
                            app_label = 'guitars'
                            abstract = False

                    print(sorted(error.id for error in run_checks()))
                """),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == '[]'
