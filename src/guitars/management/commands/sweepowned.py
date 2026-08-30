"""Repairs owned dependents left live under dead owners -- the wreckage of the per-statement
hole the 2.6.0 sweep trigger closes (issue #40, ADR 0014). A database migrated before that
trigger landed can hold rows nothing will ever stamp again; this finds and stamps them."""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import Q
from django.utils import timezone

from guitars.introspection import (
    column_owner,
    owned_tenancy_refusals,
    owner_arms,
    rule_update_cycle_edges,
)
from guitars.management import _generator
from guitars.management.enforcement.operations import _owned_rule_name
from guitars.models.soft_deletion import _owned_fields
from guitars.sql._identifiers import _unescape_ident
from guitars.tenancy import tenancy_bypassed


class Command(BaseCommand):
    """Finds -- and with ``--repair``, stamps -- orphaned owned dependents."""

    help = (
        'Reports owned rows left live under dead owners, and with --repair soft-deletes '
        'them (see docs/owned-relations.md).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'args',
            metavar='app_label',
            nargs='*',
            help=(
                'Optional app labels to scope the sweep to, matched against the app of '
                'the *dependent* being repaired rather than of the owner above it '
                '(default: every dependent of an owned rule this database holds).'
            ),
        )
        parser.add_argument(
            '--database',
            default=DEFAULT_DB_ALIAS,
            help='Database alias to sweep (default: "default").',
        )
        parser.add_argument(
            '--repair',
            action='store_true',
            dest='repair',
            help=(
                'Stamp the rows found instead of only reporting them. Off by default: this '
                'writes _deleted_at on rows the database currently shows as live, and only '
                'the operator knows whether the archived owners above them are expected.'
            ),
        )

    @staticmethod
    def _owned_rules_in_database(using: str) -> set[tuple[str, str]]:
        """``(table, rule_name)`` for every owned rule the database actually holds, under both
        the bare and the schema-qualified table name."""
        with connections[using].cursor() as cursor:
            cursor.execute(
                'SELECT schemaname, tablename, rulename FROM pg_rules '
                "WHERE rulename LIKE 'soft/_delete/_owned/_%' ESCAPE '/'"
            )
            live = set()
            for schema, table, rule in cursor.fetchall():
                live.update({(table, rule), (f'{schema}.{table}', rule)})
            return live

    @classmethod
    def _rule_carrying_owners(cls, using: str):
        """``dependent_model -> [(owner_model, fk_field_name)]`` for every relation that
        carries a rule, through ``_owned_fields`` so the verdicts are the generator's own."""
        # Repairing a relation it refused destroys what that refusal spared -- the mistake
        # 2.4.1 exists for. A refused owner stamped nothing going away; nor does this.
        registry = list(django_apps.get_models())
        cycles = rule_update_cycle_edges(registry)
        refusals = owned_tenancy_refusals(registry)
        # What the generator would emit *today* is the wrong question: an app generated under
        # a scoped run, or dropped from LOCAL_APPS since, keeps a live rule that leaks and
        # needs repairing. The database's own answer covers both, and invents nothing.
        live = cls._owned_rules_in_database(using)
        owners: dict[type, list[tuple[type, str]]] = {}
        for model in registry:
            for field in _owned_fields(model, cycles, refusals):
                dependent = column_owner(field.related_model, '_deleted_at')
                rule = _owned_rule_name(dependent._meta.db_table, field.column)
                owner_table = column_owner(model, '_deleted_at')._meta.db_table
                if (owner_table, _unescape_ident(rule[1:-1])) not in live:
                    continue
                owners.setdefault(dependent, []).append((model, field.name))
        return owners

    def _orphans(self, dependent, arms, rule_owners, using: str):
        """Live rows of *dependent* no owner still holds but an archived owner once did."""
        # Both halves matter: without the second a row nobody ever owned reads as an orphan,
        # no rule having touched it; without the first, one a live owner still holds does --
        # which is the data loss the last-owner guard exists to prevent.
        candidates = dependent._all_objects.using(using).filter(_deleted_at__isnull=True)
        # Sparing reads *every* owning column, not only rule-carrying ones: an owner refused
        # a rule still owns what it points at. ``_deleted_at`` on an MTI child resolves to the
        # ancestor's column through the ORM -- the join the rule's arm spells out by hand.
        for owner_model, fk_name in arms:
            live = (
                owner_model._all_objects.using(using)
                .filter(_deleted_at__isnull=True, **{f'{fk_name}__isnull': False})
                .values(fk_name)
            )
            candidates = candidates.exclude(pk__in=live)
        was_owned = Q(pk__in=[])
        for owner_model, fk_name in rule_owners:
            was_owned |= Q(
                pk__in=owner_model._all_objects.using(using)
                .filter(_deleted_at__isnull=False, **{f'{fk_name}__isnull': False})
                .values(fk_name)
            )
        return candidates.filter(was_owned)

    def handle(self, *app_labels, **options):
        using = options['database']
        repair = options['repair']
        requested = set(app_labels)
        # A typo'd label would otherwise match no app and report "nothing to repair" -- a
        # green gate that swept nothing. Same validation as `makeguitarmigrations`.
        _generator.validate_app_labels(requested)

        rule_owners = self._rule_carrying_owners(using)
        # The sparing half, deliberately wider than the stamping half above: ``owner_arms`` is
        # the very sweep the rule's own last-owner guard is built from, so an owner the
        # generator refused a rule still counts here -- it owns what it points at either way.
        arms = owner_arms(django_apps.get_models())

        findings: list[str] = []
        repaired = 0
        # Tables, not models: the report says "table(s)", and two models can resolve to one.
        checked: set[str] = set()
        # Tenancy bypassed: a policy hiding a live owner would manufacture an orphan and
        # stamp a still-owned row -- what the co-owner tenancy refusal prevents, unrefusable
        # here. Needs a role that sees every tenant; see docs/owned-relations.md.
        with tenancy_bypassed():
            for dependent, owners in sorted(rule_owners.items(), key=_by_label):
                if requested and dependent._meta.app_label not in requested:
                    continue
                dependent_table = dependent._meta.db_table
                checked.add(dependent_table)
                arm_pairs = [
                    (arm.owner_model, arm.fk_name) for arm in arms.get(dependent_table, ())
                ]
                orphans = self._orphans(dependent, arm_pairs, owners, using)
                pks = list(orphans.values_list('pk', flat=True))
                if not pks:
                    continue
                findings.append(
                    f'{dependent._meta.label} ({dependent_table}): {len(pks)} row(s) live '
                    f'with no live owner and at least one archived owner.'
                )
                if repair:
                    with transaction.atomic(using=using):
                        # The predicate re-asked at UPDATE time, not the pks read before it:
                        # a concurrent transaction committing a live owner in between would
                        # otherwise have its dependent stamped on a verdict already stale.
                        repaired += (
                            self._orphans(dependent, arm_pairs, owners, using)
                            .filter(pk__in=pks)
                            .update(_deleted_at=timezone.now())
                        )

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f'Owned sweep on {connections[using].alias}: '
                f'{len(checked)} dependent table(s) checked, '
                f'{len(findings)} with orphaned rows'
                + (f', {repaired} row(s) stamped.' if repair else '.')
            )
        )
        for line in findings:
            self.stdout.write(self.style.SUCCESS(line) if repair else self.style.WARNING(line))

        if findings and not repair:
            # A CommandError so this works as a CI gate; --repair turns the same finding into
            # a success, the rows having been dealt with.
            raise CommandError(
                f'{len(findings)} owned dependent table(s) hold rows no owner still holds. '
                f'Re-run with --repair to stamp them.'
            )
        self.stdout.write(self.style.SUCCESS('Owned sweep complete.'))


def _by_label(item) -> str:
    """Sort key for the report: by model label, so a run's output is stable rather than
    following registry order."""
    return item[0]._meta.label
