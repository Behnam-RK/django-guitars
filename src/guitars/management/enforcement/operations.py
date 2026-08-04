"""Building enforcement operations for an app's models.

The write side: turning what the models declare (an ``_updated_at``/``_deleted_at``
column, a ``TenantedManager``) into the ``RunSQL`` operation snippets a migration needs,
by comparing against what :func:`guitars.management.enforcement.scanning.scan_existing_operations`
already found on disk.

``OperationsMixin`` is a mixin, not a standalone class: every method here reads or writes
state that belongs to the command as a whole (``self.existing``, ``self._tenancy_notes``,
``self.stdout``), so ``Command`` (in ``command.py``) inherits from this rather than each
method taking that state as an explicit argument -- the state is one command run's worth of
bookkeeping, not data these methods are meaningfully reusable over independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, cast

from django.apps import apps as django_apps
from django.db import models

from guitars import sql
from guitars.introspection import column_owner, has_column, is_mti_child, owns_column
from guitars.management import _generator
from guitars.management.enforcement.headers import (
    _RE_MTI_UPDATED_AT,
    _RE_UPDATED_AT,
    HEADER_MTI_SOFT_DELETE,
    HEADER_MTI_UPDATED_AT,
    HEADER_SOFT_DELETE,
    HEADER_SOFT_DELETE_RELATED,
    HEADER_SOFT_DELETE_RELATED_VIA,
    HEADER_TENANT_FORCE,
    HEADER_TENANT_POLICY,
    HEADER_TENANT_POLICY_REPLACED,
    HEADER_UPDATED_AT,
)
from guitars.management.enforcement.identity import _literal, _operation
from guitars.sql import _identifiers
from guitars.tenancy.discovery import app_coverage


if TYPE_CHECKING:
    from collections.abc import Callable

    from django.apps import AppConfig
    from django.core.management.base import OutputWrapper
    from django.core.management.color import Style

    from guitars.management.enforcement.scanning import ExistingOperations
    from guitars.tenancy.discovery import TableCoverage


class _OperationRow(NamedTuple):
    """One row bound for :meth:`Command._append_if_stale`, built by :meth:`_build_operations`.

    Named rather than passed as separate positional arguments at each of the four call
    sites (trigger own-table/MTI, soft-delete own-table/MTI): those four sites differ only
    in which constants they name, so building one of these and routing it through a single
    loop reads that difference at a glance instead of repeating the call shape four times.
    """

    recorded: dict
    key: object
    header: str
    forward: str | list[str]
    reverse: str | list[str]
    replace: str | list[str] | None = None
    adopt: str | list[str] | None = None


class OperationsMixin:
    """Per-app enforcement-operation building, shared by Command via multiple inheritance."""

    if TYPE_CHECKING:
        # Provided by Command (command.py) once the two are combined -- declared here only
        # so the type checker knows what `self` carries when this mixin's own methods
        # reference it. No runtime presence: Command's real attributes/methods are what
        # actually get used, this block only teaches the checker the shape to expect.
        existing: ExistingOperations
        stdout: OutputWrapper
        stderr: OutputWrapper
        style: Style
        _tenancy_notes: list[str]
        _mti_cascade_warnings: list[str]
        trigger_function_dependency: tuple[str, str] | None
        parent_trigger_function_dependency: tuple[str, str] | None
        reverse_relations_mapping: dict[type[models.Model], set]

        @staticmethod
        def _write_migration_file(
            app: AppConfig,
            migration_file: str,
            operations: list[str],
            operations_digest: str,
            dependencies: list[tuple[str, str]] | None = None,
        ) -> None: ...
        @staticmethod
        def _tenant_policies_enabled() -> bool: ...
        @staticmethod
        def _rls_force_enabled() -> bool: ...
        @staticmethod
        def _rls_exempt_roles() -> list[str]: ...

    def _policy_identity(self, table: str, coverage: TableCoverage) -> str:
        """Digest of everything that determines what the ``tenant_scope`` policy *says*.

        Stamped into the operation's comment header so a later run can tell "this table has a
        policy" from "this table has the policy the models currently imply" -- the distinction
        the header lacked, which let a model gain a tenant dimension while the database kept
        predicating on the old one, with ``--check`` reporting nothing to do.

        ``force`` is deliberately **excluded**. It is an ``ALTER TABLE``, not part of the
        policy definition, and it already has a staged mechanism of its own
        (``--force-rls`` reading ``unforced_policies``). Folding it in would make flipping
        ``GUITARS_RLS_FORCE`` emit a full policy replacement for every table and defeat the
        retrofit workflow that setting exists for.

        Rendered through ``_literal``, so dicts are sorted and the digest is stable across
        runs -- an unstable identity would write a new migration every time.
        """
        identity = {
            'table': table,
            **coverage.as_kwargs(),
            'exempt_roles': self._rls_exempt_roles(),
        }
        return _generator.digest_of([_literal(identity)])[:12]

    def _tenant_policy_operation(
        self, table: str, coverage: TableCoverage, *, replacing: bool
    ) -> tuple[str, str]:
        """One ``tenant_scope`` policy operation, with the environment decisions baked in.

        ``force`` and ``exempt_roles`` are resolved from settings **here**, at generation
        time, and the resulting statements are written into the migration literally, so the
        same migration history always produces the same database -- see ``guitars.sql.policy``.

        *replacing* picks the ``replace_table_rls`` form, for a table that already has a
        policy of a different shape. Its ``reverse_sql`` drops RLS rather than restoring the
        previous predicate, which the generator does not know: reversing past this migration
        therefore leaves the table unpolicied, and rolling forward again rebuilds the current
        shape. That is the honest reverse -- the alternative is a reverse that claims to
        restore a policy it cannot reconstruct.

        The digest returned is over the ``create`` form either way, for the reason given in
        :func:`_operation`: keying it to the emitted form would make every run after a
        replacement disagree with the one before it.
        """
        # Kept out of the coverage mapping rather than merged into it: the coverage kwargs are
        # a typed shape describing what the policy predicates on, while these two are
        # environment decisions resolved here so they can be written into the SQL literally.
        exempt_roles = self._rls_exempt_roles() or None
        force = self._rls_force_enabled()
        coverage_kwargs = coverage.as_kwargs()

        forward = sql.create_table_rls(
            table=table, force=force, exempt_roles=exempt_roles, **coverage_kwargs
        )
        reverse = sql.drop_table_rls(table=table, exempt_roles=exempt_roles)
        header = (HEADER_TENANT_POLICY_REPLACED if replacing else HEADER_TENANT_POLICY).format(
            table=table, identity=self._policy_identity(table, coverage)
        )
        return _operation(
            header,
            forward,
            reverse,
            emit=sql.replace_table_rls(
                table=table, force=force, exempt_roles=exempt_roles, **coverage_kwargs
            )
            if replacing
            else None,
        )

    def _tenant_force_operations(self, app: AppConfig) -> list[str]:
        """FORCE-only operations for *app* -- the ``--force-rls`` retrofit stage.

        Only ever touches a table already policied whose policy shipped without FORCE
        inline (``GUITARS_RLS_FORCE`` was ``False`` at the time). New policies emit FORCE
        themselves, so this is purely the legacy backlog.
        """
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table in sorted(coverage.tables):
            # Three ways there is nothing to do, and each would otherwise write a
            # migration that changes nothing:
            #   * FORCE already has its own operation;
            #   * no policy operation exists yet -- a coverage gap FORCE must not paper
            #     over by forcing a table that nothing scopes;
            #   * the policy shipped with FORCE inline, which is the default.
            if (
                table in self.existing.tenant_forces
                or table not in self.existing.tenant_policies
                or table not in self.existing.unforced_policies
            ):
                continue
            force_source, _ = _operation(
                HEADER_TENANT_FORCE.format(table=table),
                sql.force_rls(table=table),
                sql.no_force_rls(table=table),
            )
            operations.append(force_source)
        return operations

    def _tenant_policy_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Tenant-policy create/replace operations *app* is missing or has outdated."""
        if not self._tenant_policies_enabled():
            return []

        coverage = app_coverage(app)
        self._tenancy_notes.extend(coverage.notes)

        operations: list[str] = []
        for table, table_coverage in sorted(coverage.tables.items()):
            # Two independent reasons to replace, and both must be checked. The identity
            # answers "does the policy still say what the models imply" -- a dimension added
            # or dropped, a tenant column renamed, an exempt role edited. The SQL digest
            # answers "is the text the kit emits today the text that was written". Checking
            # only the first is how a policy kept an outdated predicate; checking only the
            # second would miss a shape change that happens to render the same length of SQL.
            recorded_identity = self.existing.tenant_policy_identities.get(table)
            current_identity = self._policy_identity(table, table_coverage)
            create_source, create_digest = self._tenant_policy_operation(
                table, table_coverage, replacing=False
            )

            if adopt:
                # The premise of --adopt is a policy that exists in the database but was
                # never recorded here, and PostgreSQL has no CREATE POLICY IF NOT EXISTS --
                # so the CREATE form would fail migrate with "policy tenant_scope already
                # exists". The replace form drops first and is correct either way, which is
                # the only thing the generator can honestly assume at this point.
                replace_source, _ = self._tenant_policy_operation(
                    table, table_coverage, replacing=True
                )
                operations.append(replace_source)
            elif recorded_identity is None:
                operations.append(create_source)
            elif (
                recorded_identity != current_identity
                or self.existing.tenant_policy_sql.get(table) != create_digest
            ):
                replace_source, _ = self._tenant_policy_operation(
                    table, table_coverage, replacing=True
                )
                operations.append(replace_source)
        return operations

    def _append_if_stale(
        self,
        operations: list[str],
        recorded: dict,
        key,
        header: str,
        forward: str | list[str],
        reverse: str | list[str],
        *,
        is_adopt: bool = False,
        replace: str | list[str] | None = None,
        adopt: str | list[str] | None = None,
    ) -> None:
        """Append one operation to *operations* unless the recorded one is already current.

        *is_adopt* is the ``--adopt`` flag; *adopt* (confusingly, but this is the name the
        SQL forms below already use) is the SQL text to emit under it. Which of the three
        forms is written is decided by what the migration history knows, and the distinction
        is deliberate rather than a matter of taste -- ``IF EXISTS`` and ``OR REPLACE`` are
        claims about knowledge, and using them where the answer is known turns "your database
        has diverged from its history" into silence:

        * **Nothing recorded** -> the plain ``forward`` form. A collision then fails
          ``migrate`` loudly, which for an unqualified public-schema name like
          ``set_updated_at()`` is exactly what should happen rather than guitars quietly
          clobbering something that is not ours.
        * **Recorded, but stale** (a different digest, or none at all because the header
          predates the token) -> *replace*. The object is known to be ours, so the drop is
          unguarded and a database that has diverged says so.
        * **``--adopt``** -> *adopt*, the only form carrying ``IF EXISTS``. There the
          premise of the flag is that nobody knows what the database already has, so the
          uncertainty is real and was opted into explicitly.

        Kinds with no *replace*/*adopt* form fall back to *forward*, which is correct for
        anything created ``OR REPLACE`` in the first place.
        """
        source, digest = _operation(header, forward, reverse)
        if is_adopt:
            source, _ = _operation(header, forward, reverse, emit=adopt or replace or forward)
        elif key not in recorded:
            pass  # `source` already holds the create form.
        elif recorded[key] == digest:
            return
        else:
            source, _ = _operation(header, forward, reverse, emit=replace or forward)
        operations.append(source)

    @staticmethod
    def _mti_context(model: type[models.Model], table: str, column: str) -> dict[str, str]:
        """The ``{child_table, child_pk, parent_table, parent_pk}`` an MTI operation needs.

        Parametrized on *column* rather than computed once per model: an MTI child's
        ``_updated_at`` and ``_deleted_at`` are resolved independently via
        :func:`column_owner`, and nothing guarantees the same ancestor owns both, so this
        must be called once per column rather than shared across the two kinds below.

        ``.pk.column`` is typed ``str | None`` by Django's own stubs (a field can exist
        without a resolved column), but every concrete, registered model passed in here
        always has one -- cast rather than re-validate a framework guarantee already relied
        on everywhere else this attribute is read.
        """
        owner = column_owner(model, column)
        return {
            'child_table': table,
            'child_pk': cast(str, model._meta.pk.column),
            'parent_table': owner._meta.db_table,
            'parent_pk': cast(str, owner._meta.pk.column),
        }

    def _build_operations(self, app: AppConfig, *, adopt: bool = False) -> list[str]:
        """Return a list of SQL operation snippets needed for *app*'s models."""
        operations: list[str] = []
        deferred: list[str] = []

        for model in app.get_models():
            table = model._meta.db_table
            # The *column*, not the field name. The two agree for the ordinary ``id`` primary
            # key, which is why this went unnoticed -- but a model whose pk sets ``db_column``,
            # or is a ``OneToOneField(primary_key=True)`` (name ``owner``, column
            # ``owner_id``), would have produced a rule referencing a column that does not
            # exist and failed at ``migrate``. The MTI branches below already used ``.column``.
            # Cast rather than re-validate: see _mti_context's docstring on why `.pk.column`
            # is always populated for a concrete, registered model despite its stub type.
            primary_key = cast(str, model._meta.pk.column)

            rows: list[_OperationRow] = []

            # --- updated_at trigger: own table vs. MTI parent-propagation ---
            # `table`/`child_table` are bare-identifier template positions (escaped via
            # _escape_ident); `primary_key`/`parent_table`/`parent_pk`/`child_pk` are
            # string-literal arguments to set_updated_at()/set_parent_updated_at()
            # (escaped via _escape_literal) -- see triggers.py's module docstring.
            if owns_column(model, '_updated_at'):
                ident_table = _identifiers._escape_ident(table)
                literal_primary_key = _identifiers._escape_literal(primary_key)
                rows.append(
                    _OperationRow(
                        recorded=self.existing.triggers,
                        key=table,
                        header=HEADER_UPDATED_AT.format(table=table),
                        forward=sql.CREATE_UPDATED_AT_TRIGGER.format(
                            table=ident_table, primary_key=literal_primary_key
                        ),
                        reverse=sql.DROP_UPDATED_AT_TRIGGER.format(table=ident_table),
                        replace=sql.REPLACE_UPDATED_AT_TRIGGER.format(
                            table=ident_table, primary_key=literal_primary_key
                        ),
                        adopt=sql.ADOPT_UPDATED_AT_TRIGGER.format(
                            table=ident_table, primary_key=literal_primary_key
                        ),
                    )
                )
            elif is_mti_child(model, '_updated_at'):
                mti = self._mti_context(model, table, '_updated_at')
                mti_sql = {
                    'child_table': _identifiers._escape_ident(mti['child_table']),
                    'parent_table': _identifiers._escape_literal(mti['parent_table']),
                    'parent_pk': _identifiers._escape_literal(mti['parent_pk']),
                    'child_pk': _identifiers._escape_literal(mti['child_pk']),
                }
                rows.append(
                    _OperationRow(
                        recorded=self.existing.mti_triggers,
                        key=table,
                        header=HEADER_MTI_UPDATED_AT.format(**mti),
                        forward=sql.CREATE_PARENT_UPDATED_AT_TRIGGER.format(**mti_sql),
                        reverse=sql.DROP_PARENT_UPDATED_AT_TRIGGER.format(
                            child_table=_identifiers._escape_ident(table)
                        ),
                        replace=sql.REPLACE_PARENT_UPDATED_AT_TRIGGER.format(**mti_sql),
                        adopt=sql.ADOPT_PARENT_UPDATED_AT_TRIGGER.format(**mti_sql),
                    )
                )

            # --- soft-delete rule: own table vs. MTI redirect-to-owner ---
            # Rules need no replace or adopt form: they are created ``OR REPLACE``, which is
            # not defensiveness but the only safe way to redefine one -- an instant without a
            # ``soft_delete`` rule is an instant in which DELETE destroys rows.
            # --- soft-delete rule: every placeholder here is a bare-identifier position
            #     (escaped via _escape_ident) -- soft_delete.py's templates have no
            #     string-literal-argument positions, unlike the trigger templates above.
            if owns_column(model, '_deleted_at'):
                rows.append(
                    _OperationRow(
                        recorded=self.existing.soft_deletes,
                        key=table,
                        header=HEADER_SOFT_DELETE.format(table=table),
                        forward=sql.CREATE_SOFT_DELETE_RULE.format(
                            table=_identifiers._escape_ident(table),
                            primary_key=_identifiers._escape_ident(primary_key),
                        ),
                        reverse=sql.DROP_SOFT_DELETE_RULE.format(
                            table=_identifiers._escape_ident(table)
                        ),
                    )
                )
            elif is_mti_child(model, '_deleted_at'):
                mti = self._mti_context(model, table, '_deleted_at')
                mti_sql = {key: _identifiers._escape_ident(value) for key, value in mti.items()}
                rows.append(
                    _OperationRow(
                        recorded=self.existing.mti_soft_deletes,
                        key=table,
                        header=HEADER_MTI_SOFT_DELETE.format(**mti),
                        forward=sql.CREATE_MTI_SOFT_DELETE_RULE.format(**mti_sql),
                        reverse=sql.DROP_MTI_SOFT_DELETE_RULE.format(
                            child_table=_identifiers._escape_ident(table)
                        ),
                    )
                )

            for row in rows:
                self._append_if_stale(
                    operations,
                    row.recorded,
                    row.key,
                    row.header,
                    row.forward,
                    row.reverse,
                    is_adopt=adopt,
                    replace=row.replace,
                    adopt=row.adopt,
                )

            # --- cascade rules for CASCADE FKs pointing at this model (deferred so they
            #     always follow the owner's own soft-delete rule) ---
            if has_column(model, '_deleted_at'):
                deferred.extend(self._cascade_operations(model, adopt=adopt))

        # Tenant policies last: they are independent of the triggers and rules above (a
        # policy references neither), so they sort to the end where they read as a group.
        return operations + deferred + self._tenant_policy_operations(app, adopt=adopt)

    @staticmethod
    def _is_cascade_candidate(related_model, fk_field, on_delete) -> bool:
        """Whether this reverse relation is one a cascade soft-delete rule is written for.

        Shared by :meth:`_cascade_operations`, which writes the rules, and
        :meth:`_scoped_cascade_gap_notes`, which reports the ones a scoped run left out.
        Shared rather than restated, for the same reason ``tenancy.discovery`` is: the two
        had drifted, and a gap note for a relation the generator would never write a rule
        for promises the reader that naming the parent's app closes it, which is false.
        Only the remaining ``owns_column`` test is left to the callers, because the writer
        warns about it and the reporter must simply stay quiet.
        """
        return (
            on_delete == models.CASCADE
            and has_column(related_model, '_deleted_at')
            # The MTI parent-link (a CASCADE OneToOne) is structural, not a user cascade FK.
            and not getattr(fk_field.remote_field, 'parent_link', False)
            # An FK reached through MTI is not a second FK: it is the *same physical column*
            # on the ancestor's table, which appears in the caller's loop in its own right.
            and fk_field.model is related_model
        )

    def _cascade_candidates(
        self, model: type[models.Model], owner_table: str
    ) -> list[tuple[type[models.Model], models.ForeignKey, bool]]:
        """CASCADE FKs pointing at *model*, each flagged whether it is the *primary* one
        for its related_table.

        Shared by :meth:`_cascade_operations` (which writes the rules) and
        :meth:`_scoped_cascade_gap_notes` (which reports the ones an out-of-scope run
        leaves out), for the same reason :meth:`_is_cascade_candidate` is shared: the two
        must agree not just on *which* FKs count but on *which dedupe key* each one uses,
        or a report and a write disagree about what is already covered.

        Two or more CASCADE FKs from the same related_table to the same owner table need
        distinct dedupe keys -- and, when the rules are actually written, distinct rule
        names (see ``sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA``'s comment for why a
        shared name silently orphans one FK's cascade). The first one, in the
        deterministic ``(related_table, column)`` sort order below, is primary and keeps
        the plain, historical form -- so every already-migrated project's lone cascade
        rule for a pair is untouched and never re-emitted. Only a genuine second-or-later
        FK on the same pair is not primary.
        """
        seen_related_tables: set[str] = set()
        candidates: list[tuple[type[models.Model], models.ForeignKey, bool]] = []
        for related_model, fk_field, on_delete in sorted(
            self.reverse_relations_mapping[model],
            key=lambda t: (t[0]._meta.db_table, t[1].column),
        ):
            # Structural parent-links and MTI-inherited FKs are both excluded there: the MTI
            # redirect rule already ties a child's deletion to the owner, and an inherited FK
            # is the ancestor's own column, whose rule this same loop emits -- and because
            # every table in an MTI chain shares one ``_deleted_at``, that rule already
            # archives the children.
            if not self._is_cascade_candidate(related_model, fk_field, on_delete):
                continue
            related_table = related_model._meta.db_table
            # The flat cascade rule does ``UPDATE related_table SET _deleted_at`` -- only valid
            # when the related child owns ``_deleted_at`` on the very table its FK lives on.
            # An FK declared on an MTI child's *own* table while its ``_deleted_at`` lives on a
            # farther ancestor needs a join form we don't emit yet; surface it instead of
            # writing a rule that references a missing column.
            if not owns_column(related_model, '_deleted_at'):
                self._mti_cascade_warnings.append(
                    f"Cascade rule for '{related_table}' -> '{owner_table}' skipped: "
                    f"'{related_model.__name__}' declares this foreign key on its own table "
                    'but inherits _deleted_at from a multi-table-inheritance ancestor, which '
                    'needs a join form the generator does not emit yet.'
                )
                continue
            is_primary = related_table not in seen_related_tables
            seen_related_tables.add(related_table)
            candidates.append((related_model, fk_field, is_primary))
        return candidates

    def _cascade_operations(self, model: type[models.Model], *, adopt: bool = False) -> list[str]:
        """Cascade soft-delete rules for ``on_delete=CASCADE`` FKs pointing at *model*.

        The rule is an ``ON UPDATE`` rule that must live on the table whose ``_deleted_at``
        column actually flips: *model*'s own table for the single-table case, or the owning
        ancestor when *model* is an MTI child (an ``ON UPDATE TO child_table`` rule would never
        fire, since the child table's ``_deleted_at`` is never written). The related child's FK
        column holds the shared MTI pk value, so matching it against the owner pk still works.
        """
        owner = column_owner(model, '_deleted_at')
        owner_table = owner._meta.db_table
        owner_pk = cast(str, owner._meta.pk.column)

        ops: list[str] = []
        for related_model, fk_field, is_primary in self._cascade_candidates(model, owner_table):
            related_table = related_model._meta.db_table
            # Headers stay on the raw (unescaped) names -- they're comments the scanner
            # regex-matches back to a table name, not SQL, so escaping them would break
            # that round-trip. The SQL itself uses the escaped forms throughout.
            ident_owner_table = _identifiers._escape_ident(owner_table)
            ident_related_table = _identifiers._escape_ident(related_table)
            ident_owner_pk = _identifiers._escape_ident(owner_pk)
            ident_foreign_key = _identifiers._escape_ident(fk_field.column)
            if is_primary:
                key = (related_table, owner_table, None)
                header = HEADER_SOFT_DELETE_RELATED.format(
                    related_table=related_table, table=owner_table
                )
                forward = sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                    table=ident_owner_table,
                    related_table=ident_related_table,
                    primary_key=ident_owner_pk,
                    foreign_key=ident_foreign_key,
                )
                reverse = sql.DROP_SOFT_DELETE_RELATED_OBJECTS_RULE.format(
                    table=ident_owner_table, related_table=ident_related_table
                )
            else:
                key = (related_table, owner_table, fk_field.column)
                header = HEADER_SOFT_DELETE_RELATED_VIA.format(
                    related_table=related_table, table=owner_table, foreign_key=fk_field.column
                )
                forward = sql.CREATE_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA.format(
                    table=ident_owner_table,
                    related_table=ident_related_table,
                    primary_key=ident_owner_pk,
                    foreign_key=ident_foreign_key,
                )
                reverse = sql.DROP_SOFT_DELETE_RELATED_OBJECTS_RULE_VIA.format(
                    table=ident_owner_table,
                    related_table=ident_related_table,
                    foreign_key=ident_foreign_key,
                )
            self._append_if_stale(
                ops,
                self.existing.soft_delete_related,
                key,
                header,
                forward,
                reverse,
                is_adopt=adopt,
            )
        return ops

    def _scoped_cascade_gap_notes(self, requested: set[str]) -> list[str]:
        """Describe cross-app CASCADE soft-delete rules this scoped run will not create.

        A rule like "deleting Band cascades to Album" is generated while
        ``_build_operations`` processes Band's app (the *parent* holding
        ``_deleted_at``), not Album's — so if the parent's app is scoped out,
        the rule is skipped even when the child's app is in scope. This is the
        intended "pragmatic scope" tradeoff (mirrors Django, which also only
        touches the apps you name), not a bug; it's closed by a later run that
        includes the parent's app label (or no labels at all).

        Only reported when the *child* (related) model's app is itself part of
        this scoped run — otherwise the gap is about two apps neither of which
        the caller asked to generate migrations for, which is just noise.
        """
        if not requested:
            return []

        model_app_label = {
            model: app.label
            for app in django_apps.get_app_configs()
            if _generator.is_local(app)
            for model in app.get_models()
        }

        notes: list[str] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_local(app) or app.label in requested:
                continue
            for model in app.get_models():
                if not has_column(model, '_deleted_at'):
                    continue
                # The rule lives on the table that owns _deleted_at (the model itself, or its
                # MTI ancestor), matching where `_cascade_operations` places it.
                table = column_owner(model, '_deleted_at')._meta.db_table
                # Shared with _cascade_operations, which is what makes "closed by a later run
                # naming the parent's app" a promise this check can actually verify: the two
                # must agree on both which FKs count and which dedupe key each one uses.
                for related_model, fk_field, is_primary in self._cascade_candidates(model, table):
                    if model_app_label.get(related_model) not in requested:
                        continue
                    related_table = related_model._meta.db_table
                    key = (related_table, table, None if is_primary else fk_field.column)
                    if key in self.existing.soft_delete_related:
                        continue
                    notes.append(
                        f"Cascade rule on '{related_table}' related to '{table}' skipped: "
                        f"parent app '{app.label}' is not in this scoped run."
                    )
        return notes

    def _function_dependencies_for(self, operations_blob: str) -> list[tuple[str, str]]:
        """Function-migration dependencies an app's operations actually require.

        Only ``updated_at`` triggers call a shared trigger function: own-table triggers use
        ``set_updated_at`` (the base function migration), MTI parent-propagation triggers use
        ``set_parent_updated_at`` (the parent function migration). Soft-delete and cascade rules
        call no function, so an app emitting only those needs neither dependency. Keying off the
        operation headers (rather than appending both deps unconditionally) keeps an app's
        migration from being coupled to a function migration -- and its host app's ordering --
        it never uses.
        """
        deps: list[tuple[str, str]] = []
        if self.trigger_function_dependency and _RE_UPDATED_AT.search(operations_blob):
            deps.append(self.trigger_function_dependency)
        if self.parent_trigger_function_dependency and _RE_MTI_UPDATED_AT.search(operations_blob):
            deps.append(self.parent_trigger_function_dependency)
        return deps

    def _generate_stage(
        self,
        requested: set[str],
        *,
        migration_name: str,
        build_ops: Callable[[AppConfig], list[str]],
        check_only: bool,
        dependencies_for: Callable[[str], list[tuple[str, str]]] | None = None,
    ) -> tuple[bool, list[tuple[str, list[str]]]]:
        """Scaffold-and-write one migration per in-scope app whose operations are new.

        Shared by ``handle()``'s per-app loop and :meth:`_handle_force_rls_stage`'s -- the
        two used to be an almost line-for-line copy of each other, differing only in which
        method built the operations, what name the scaffolded migration got, and whether it
        declared a dependency on the singleton trigger-function migration(s).

        Returns ``(changes_made, check_missing)`` for the caller to fold into its own
        bookkeeping rather than writing either itself: the two callers flush different sets
        of warning-note lists afterward (``handle()`` flushes three, the FORCE stage only
        one), and getting that difference right matters more than deduplicating it away too.
        """
        changes_made = False
        check_missing: list[tuple[str, list[str]]] = []
        for app in django_apps.get_app_configs():
            if not _generator.is_in_scope(app, requested):
                continue

            operations = build_ops(app)
            if not operations:
                continue

            operations_digest = _generator.digest_of(operations)
            if operations_digest in self.existing.existing_digests.get(app.label, set()):
                continue

            if check_only:
                check_missing.append((app.label, operations))
                continue

            migration_file = _generator.create_empty_migration_file(app, migration_name)
            dependencies = dependencies_for('\n'.join(operations)) if dependencies_for else None
            self._write_migration_file(
                app=app,
                migration_file=migration_file,
                operations=operations,
                operations_digest=operations_digest,
                dependencies=dependencies,
            )

            self.stdout.write(
                self.style.MIGRATE_HEADING(f"Enforcement migrations for '{app.label}':")
            )
            self.stdout.write(f'  migrations/{migration_file}')
            changes_made = True

        return changes_made, check_missing
