"""Repairs owned dependents left live under dead owners -- the wreckage of the per-statement
hole the 2.6.0 sweep trigger closes (issue #40, ADR 0014). A database migrated before that
trigger landed can hold rows nothing will ever stamp again; this finds and stamps them."""

from __future__ import annotations

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import F, Q
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
from guitars.sql._identifiers import _split_qualified, _unescape_ident
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
        unresolved: list[str] = []
        for model in registry:
            for field in _owned_fields(model, cycles, refusals):
                dependent = column_owner(field.related_model, '_deleted_at')
                owner_table = column_owner(model, '_deleted_at')._meta.db_table
                # ``pg_rules`` reports bare identifiers; a ``db_table`` may carry Django's own
                # pre-quoted or self-quoted form. Compared raw, such a table matches nothing,
                # every relation on it is dropped, and the run reports a sweep of none of them.
                try:
                    rule = _owned_rule_name(dependent._meta.db_table, field.column)
                    schema, bare = _split_qualified('table', owner_table)
                except ValueError as exc:
                    # Named, never swallowed: a `db_table` this cannot spell is one relation
                    # skipped, and the operator has to know which. Raising instead would take
                    # the whole report down over a table the run may not even have needed.
                    unresolved.append(
                        f"'{model._meta.label}.{field.name}' -> '{dependent._meta.label}' "
                        f'skipped: {exc}'
                    )
                    continue
                key = bare if schema is None else f'{schema}.{bare}'
                if (key, _unescape_ident(rule[1:-1])) not in live:
                    continue
                owners.setdefault(dependent, []).append((model, field.name))
        return owners, unresolved

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
            live = owner_model._all_objects.using(using).filter(
                _deleted_at__isnull=True, **{f'{fk_name}__isnull': False}
            )
            # The rule's target exclusion, in Python: an arm taking liveness from the
            # *dependent's own* table would read a self-pointing row as its own live owner and
            # spare it for ever. Excluded by the key, exactly as the rendered arm does.
            owner_liveness_table = column_owner(owner_model, '_deleted_at')._meta.db_table
            if owner_liveness_table == dependent._meta.db_table:
                live = live.exclude(pk=F(fk_name))
            candidates = candidates.exclude(pk__in=live.values(fk_name))
        was_owned = Q(pk__in=[])
        for owner_model, fk_name in rule_owners:
            was_owned |= Q(
                pk__in=owner_model._all_objects.using(using)
                .filter(_deleted_at__isnull=False, **{f'{fk_name}__isnull': False})
                .values(fk_name)
            )
        return candidates.filter(was_owned)

    def _sweep_pass(self, rule_owners, arms, requested, using, repair):
        """One walk of every rule-carrying dependent: ``({(label, table): rows}, stamped, tables
        looked at)``. Split out of :meth:`handle` so a repair can run it again -- stamping a
        dependent can orphan what *it* owns, and the walk is ordered by label, not ownership."""
        found: dict[tuple[str, str], int] = {}
        stamped = 0
        checked: set[str] = set()
        for dependent, owners in sorted(rule_owners.items(), key=_by_label):
            if requested and dependent._meta.app_label not in requested:
                continue
            dependent_table = dependent._meta.db_table
            checked.add(dependent_table)
            arm_pairs = [(arm.owner_model, arm.fk_name) for arm in arms.get(dependent_table, ())]
            orphans = self._orphans(dependent, arm_pairs, owners, using)
            pks = list(orphans.values_list('pk', flat=True))
            if not pks:
                continue
            found[dependent._meta.label, dependent_table] = len(pks)
            if repair:
                with transaction.atomic(using=using):
                    # The predicate re-asked at UPDATE time, not the pks read before it:
                    # a concurrent transaction committing a live owner in between would
                    # otherwise have its dependent stamped on a verdict already stale.
                    stamped += (
                        self._orphans(dependent, arm_pairs, owners, using)
                        .filter(pk__in=pks)
                        .update(_deleted_at=timezone.now())
                    )
        return found, stamped, checked

    def handle(self, *app_labels, **options):
        using = options['database']
        repair = options['repair']
        requested = set(app_labels)
        # A typo'd label would otherwise match no app and report "nothing to repair" -- a
        # green gate that swept nothing. Same validation as `makeguitarmigrations`.
        _generator.validate_app_labels(requested)

        rule_owners, unresolved = self._rule_carrying_owners(using)
        # The sparing half, deliberately wider than the stamping half above: ``owner_arms`` is
        # the very sweep the rule's own last-owner guard is built from, so an owner the
        # generator refused a rule still counts here -- it owns what it points at either way.
        arms = owner_arms(django_apps.get_models())

        # ``{(label, table): rows found}`` rather than rendered lines, so a table yielding
        # fresh orphans on a later pass is one entry with a summed count rather than a second
        # line. Keyed by model as the report line is; the heading counts tables separately.
        findings: dict[tuple[str, str], int] = {}
        repaired = 0
        # Tables, not models: the report says "table(s)", and two models can resolve to one.
        checked: set[str] = set()
        # A pass repairs in model-label order, so a chain whose owner sorts *after* what it owns
        # leaves a fresh orphan behind it. The trigger settles a chain inside the one statement.

        # This command is for a database holding rules and no trigger, so it runs to a fixpoint
        # here rather than making the operator re-run to one.
        passes = max(len(rule_owners), 1) + 1
        # Tenancy bypassed: a policy hiding a live owner would manufacture an orphan and
        # stamp a still-owned row -- what the co-owner tenancy refusal prevents, unrefusable
        # here. Needs a role that sees every tenant; see docs/owned-relations.md.
        settled = True
        with tenancy_bypassed():
            for _ in range(passes):
                found, stamped, seen = self._sweep_pass(
                    rule_owners, arms, requested, using, repair
                )
                checked |= seen
                for entry, rows in found.items():
                    findings[entry] = findings.get(entry, 0) + rows
                repaired += stamped
                # Report-only describes the database as it stands, so a second look at an
                # unchanged one would only repeat itself.
                if not repair or not stamped:
                    break
            else:
                # Exhausting the bound says the run did not settle, not why: depth beyond it
                # needs a cycle of ON UPDATE rules, while fresh orphans arriving between passes
                # need only a concurrent writer -- ordinary on a busy database.

                # Reported, not raised as a diagnosis: every pass committed real repairs, and
                # naming a cycle the operator may not have would send them after nothing.
                settled = False

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f'Owned sweep on {connections[using].alias}: '
                f'{len(checked)} dependent table(s) checked, '
                f'{len({table for _, table in findings})} with orphaned rows'
                + (f', {repaired} row(s) stamped.' if repair else '.')
            )
        )
        for (label, table), rows in findings.items():
            line = (
                f'{label} ({table}): {rows} row(s) live with no live owner and at least '
                'one archived owner.'
            )
            self.stdout.write(self.style.SUCCESS(line) if repair else self.style.WARNING(line))
        # Before the gate, and on stderr: a relation this could not name was not swept, and a
        # run that stayed silent about it would report a clean sweep of a table it skipped.
        for line in unresolved:
            self.stderr.write(self.style.WARNING(line))

        if not settled:
            # Non-zero for the same reason the report-only gate is: the database is not in
            # the state a clean run leaves it in, and the operator has something to do.
            raise CommandError(
                f'The owned sweep stamped {repaired} row(s) but had not settled after '
                f'{passes} passes. Re-run --repair; if it never settles, check the database '
                'for a cycle of ON UPDATE rules, which the generator refuses but a database '
                'migrated before that refusal may still hold.'
            )
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
