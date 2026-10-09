"""What the scan does with the table events a migration makes (ADR 0043): its files are replayed
in the order ``migrate`` runs them, each one's events before its headers, so a later file wins by
being later -- a drop voids what lived on the table, a rename moves it, a retaken name is new."""

from __future__ import annotations

import types

from guitars.management import _generator
from guitars.management.enforcement import scanning
from guitars.management.enforcement.graph import TableEvent
from guitars.management.enforcement.headers import (
    HEADER_SOFT_DELETE,
    HEADER_SOFT_DELETE_OWNED_SWEEP,
    HEADER_SOFT_DELETE_RELATED,
    HEADER_SOFT_DELETE_RELATED_RETIRED,
    HEADER_TENANT_AUTOFILL,
    HEADER_TENANT_FORCE,
    HEADER_TENANT_POLICY,
    HEADER_UPDATED_AT,
)
from guitars.management.enforcement.scanning import scan_existing_operations
from tests.conftest import patch_replay
from tests.test_replay_plan import _create, _Loader


def _replay(monkeypatch, *steps, stems=None):
    """Scan synthetic files, one per step ``(content, events)``, each read at a migration whose
    events happen first, after the real project's own. *stems* names them where their order by
    name must differ from their order of replay."""
    stems = stems or [f'{index:04d}_auto_enforcement' for index in range(len(steps))]
    patch_replay(
        monkeypatch,
        *(('testapp', stem, events) for stem, (_c, events) in zip(stems, steps, strict=True)),
    )

    def _iter(app):
        if app.label == 'testapp':
            for stem, (content, _events) in zip(stems, steps, strict=True):
                yield types.SimpleNamespace(stem=stem), content

    monkeypatch.setattr(_generator, 'iter_migration_files', _iter)
    return scan_existing_operations()


def _trigger(table: str, digest: str = 'aaaaaaaaaaaa') -> str:
    return HEADER_UPDATED_AT.format(table=table) + f' [SQL:{digest}]\n'


def _rule(table: str, digest: str = 'bbbbbbbbbbbb') -> str:
    return HEADER_SOFT_DELETE.format(table=table) + f' [SQL:{digest}]\n'


def _sweep(dependent: str, owner: str, column: str = 'fk_id') -> str:
    return (
        HEADER_SOFT_DELETE_OWNED_SWEEP.format(
            dependent_table=dependent, table=owner, foreign_key=column
        )
        + ' [SQL:cccccccccccc]\n'
    )


def _related(related: str, owner: str) -> str:
    return HEADER_SOFT_DELETE_RELATED.format(related_table=related, table=owner) + ' [SQL:dddd]\n'


def _drop(table):
    return TableEvent('drop', table)


def _rename(old, new):
    return TableEvent('rename', old, new)


def _create_event(table):
    return TableEvent('create', table)


class TestADrop:
    def test_voids_what_lived_on_the_table(self, monkeypatch):
        existing = _replay(
            monkeypatch,
            (_trigger('shop_part') + _rule('shop_part') + _trigger('shop_other'), []),
            ('', [_drop('shop_part')]),
        )

        assert 'shop_part' not in existing.triggers
        assert 'shop_part' not in existing.soft_deletes
        assert 'shop_other' in existing.triggers

    def test_a_recreated_table_reads_as_uncovered_until_it_is_written_again(self, monkeypatch):
        gone = _replay(
            monkeypatch,
            (_trigger('shop_part'), []),
            ('', [_drop('shop_part')]),
            ('', [_create_event('shop_part')]),
        )
        back = _replay(
            monkeypatch,
            (_trigger('shop_part', 'old000000000'), []),
            ('', [_drop('shop_part')]),
            (_trigger('shop_part', 'new000000000'), [_create_event('shop_part')]),
        )

        assert 'shop_part' not in gone.triggers
        assert back.triggers['shop_part'] == 'new000000000'

    def test_keeps_a_rule_keyed_on_it_as_a_related_table_for_the_dropped_child_retirement(
        self, monkeypatch
    ):
        """ADR 0029: the owner's arm names the dropped child, and that retirement needs the
        record and its create to order the drop against."""
        existing = _replay(
            monkeypatch, (_related('shop_child', 'shop_owner'), []), ('', [_drop('shop_child')])
        )

        key = ('shop_child', 'shop_owner', None)
        assert key in existing.soft_delete_related
        assert existing.soft_delete_related_dependencies[key] == [
            ('testapp', '0000_auto_enforcement')
        ]

    def test_voids_a_sweep_firing_on_it_and_its_policy_and_autofill(self, monkeypatch):
        policy = HEADER_TENANT_POLICY.format(table='shop_item', identity='ident') + ' [SQL:eeee]\n'
        autofill = (
            HEADER_TENANT_AUTOFILL.format(table='shop_item', function='fill_fn') + ' [SQL:ffff]\n'
        )

        existing = _replay(
            monkeypatch,
            (_sweep('shop_target', 'shop_item') + policy + autofill, []),
            ('', [_drop('shop_item')]),
        )

        assert not existing.soft_delete_owned_sweep
        assert 'shop_item' not in existing.tenant_policies
        assert 'shop_item' not in existing.tenant_policy_identities
        assert not existing.tenant_autofill

    def test_stops_a_name_forwarding_from_where_it_went(self, monkeypatch):
        existing = _replay(
            monkeypatch,
            ('', [_rename('shop_a', 'shop_b')]),
            ('', [_drop('shop_b')]),
            (_trigger('shop_a'), []),
        )

        assert 'shop_a' in existing.triggers
        assert 'shop_b' not in existing.triggers

    def test_stops_vouching_for_the_digest_of_a_file_that_named_the_table(self, monkeypatch):
        named = '# [DIGEST:named0000000000000000000000000000]\n' + _trigger('shop_part')
        unrelated = '# [DIGEST:other00000000000000000000000000000]\n' + _trigger('shop_other')

        existing = _replay(monkeypatch, (named, []), (unrelated, []), ('', [_drop('shop_part')]))

        assert existing.existing_digests['testapp'] >= {'other00000000000000000000000000000'}
        assert 'named0000000000000000000000000000' not in existing.existing_digests['testapp']


class TestAForceOnlyFile:
    """The FORCE stage's file names a table and nothing else: its digest has to go with the table,
    or a recreated one is written unforced and the identical FORCE operation is skipped."""

    @staticmethod
    def _force(table: str) -> str:
        return '# [DIGEST:force000000000000000000000000000000]\n' + (
            HEADER_TENANT_FORCE.format(table=table) + '\n'
        )

    def test_a_drop_stops_it_vouching(self, monkeypatch):
        existing = _replay(monkeypatch, (self._force('shop_item'), []), ('', [_drop('shop_item')]))

        assert 'shop_item' not in existing.tenant_forces
        assert 'force000000000000000000000000000000' not in existing.existing_digests.get(
            'testapp', ()
        )

    def test_a_rename_away_stops_it_vouching(self, monkeypatch):
        existing = _replay(
            monkeypatch, (self._force('shop_item'), []), ('', [_rename('shop_item', 'shop_x')])
        )

        assert 'shop_x' in existing.tenant_forces
        assert 'force000000000000000000000000000000' not in existing.existing_digests.get(
            'testapp', ()
        )

    def test_a_file_with_no_digest_line_still_records_the_force(self, monkeypatch):
        existing = _replay(monkeypatch, (HEADER_TENANT_FORCE.format(table='shop_item') + '\n', []))

        assert 'shop_item' in existing.tenant_forces


class TestARename:
    def test_a_name_a_new_model_retakes_is_that_models_to_cover(self, monkeypatch):
        existing = _replay(
            monkeypatch,
            (_trigger('shop_crew') + _rule('shop_crew'), []),
            ('', [_rename('shop_crew', 'shop_squad')]),
            ('', [_create_event('shop_crew')]),
        )

        assert 'shop_squad' in existing.triggers and 'shop_squad' in existing.soft_deletes
        assert 'shop_crew' not in existing.triggers
        assert 'shop_crew' not in existing.soft_deletes
        assert existing.renamed_tables['shop_squad'] == ['shop_crew']

    def test_a_header_naming_the_emptied_name_afterwards_is_filed_under_the_new_one(
        self, monkeypatch
    ):
        existing = _replay(
            monkeypatch, ('', [_rename('shop_a', 'shop_b')]), (_trigger('shop_a'), [])
        )

        assert 'shop_b' in existing.triggers
        assert 'shop_a' not in existing.triggers

    def test_a_name_taken_again_stops_forwarding(self, monkeypatch):
        existing = _replay(
            monkeypatch,
            ('', [_rename('shop_a', 'shop_b')]),
            (_trigger('shop_a'), [_create_event('shop_a')]),
        )

        assert 'shop_a' in existing.triggers
        assert 'shop_b' not in existing.triggers

    def test_chains_follow_each_hop(self, monkeypatch):
        existing = _replay(
            monkeypatch,
            ('', [_rename('shop_a', 'shop_b')]),
            ('', [_rename('shop_b', 'shop_c')]),
            (_trigger('shop_a'), []),
        )

        assert 'shop_c' in existing.triggers
        assert existing.renamed_tables['shop_c'] == ['shop_a', 'shop_b']

    def test_a_move_there_and_back_ends_with_the_newer_record(self, monkeypatch):
        """Shape (c): the record filed before the first move is the older; the later wins."""
        existing = _replay(
            monkeypatch,
            (_sweep('shop_keeper', 'shop_a'), []),
            ('', [_rename('shop_a', 'shop_b')]),
            (_trigger('shop_b', 'mid000000000'), []),
            ('', [_rename('shop_b', 'shop_a')]),
        )

        assert existing.triggers == {'shop_a': 'mid000000000'}
        assert ('shop_keeper', 'shop_a', 'fk_id') in existing.soft_delete_owned_sweep
        assert existing.renamed_tables['shop_a'] == ['shop_b']

    def test_a_retirement_site_recorded_before_the_rename_follows_it(self, monkeypatch):
        retired = (
            HEADER_SOFT_DELETE_RELATED_RETIRED.format(related_table='shop_a', table='shop_owner')
            + ' [SQL:dddd]\n'
        )

        existing = _replay(
            monkeypatch,
            (_related('shop_a', 'shop_owner'), []),
            (retired, []),
            ('', [_rename('shop_a', 'shop_b')]),
        )

        (site,) = existing.cascade_retirement_sites
        assert site.key == ('shop_b', 'shop_owner', None)


class TestARetireEvent:
    def test_a_column_retirement_takes_only_the_keys_naming_it(self, monkeypatch):
        existing = _replay(
            monkeypatch,
            (_related('shop_child', 'shop_owner') + _trigger('shop_child'), []),
            ('', [TableEvent('retire', 'shop_child', column='owner_id')]),
        )

        assert not existing.soft_delete_related
        assert 'shop_child' in existing.triggers


def test_an_owned_header_written_twice_in_one_file_records_one_create(monkeypatch):
    existing = _replay(monkeypatch, (_sweep('shop_a', 'shop_b') * 2, []))

    assert existing.soft_delete_owned_sweep_dependencies[('shop_a', 'shop_b', 'fk_id')] == [
        ('testapp', '0000_auto_enforcement')
    ]


class TestRespelling:
    def test_each_key_shape_names_its_tables_where_it_does(self):
        forward = {'a': 'x', 'b': 'y'}

        assert scanning._respell('a', forward) == 'x'
        assert scanning._respell(('a',), forward) == ('x',)
        assert scanning._respell(('a', 'col'), forward) == ('x', 'col')
        assert scanning._respell(('a', 'b', None), forward) == ('x', 'y', None)
        assert scanning._respell(('a', 'b', 'fk'), {}) == ('a', 'b', 'fk')


class TestAReplayOrder:
    """Files the graph does not know, and files a squash replaced."""

    def test_a_squash_is_ordered_against_its_own_replaced_files_by_their_place(self):
        loader = _Loader(
            ('anc', [[_create()], [_create('other')], [_create('third')]]),
            squash=('anc', ['0001', '0002'], [_create(), _create('other')]),
        )
        files = {('anc', '0001'): '', ('anc', '0002'): '', ('anc', '0003'): ''}

        units, aliases = scanning._replay_units(loader, files)

        assert [unit.name for unit in units] == [
            '0001',
            '0002',
            '0001_squashed_0002',
            '0003',
        ]
        graph = loader.graph
        assert scanning._orders(('anc', '0001'), ('anc', '0002'), graph, aliases)
        assert not scanning._orders(('anc', '0002'), ('anc', '0001'), graph, aliases)
        assert scanning._orders(('anc', '0002'), ('anc', '0003'), graph, aliases)

    def test_a_replaced_file_beside_an_unexpanded_squash_is_read_before_it(self):
        loader = _Loader(
            ('anc', [[_create()], [_create('other')], [_create('third')]]),
            squash=('anc', ['0001', '0002'], [_create(), _create('other')]),
        )
        del loader.disk_migrations['anc', '0002']  # the squash cannot be expanded
        files = {('anc', '0001'): '', ('anc', '0003'): '', ('anc', 'extra'): ''}

        units, _aliases = scanning._replay_units(loader, files)

        assert [unit.name for unit in units] == ['0001', '0001_squashed_0002', '0003', 'extra']


class TestKeysNamingATableAndAColumn:
    def test_a_column_named_like_the_renamed_table_is_not_renamed_with_it(self, monkeypatch):
        """``_respell`` renames the table positions of a key; the third element of an owned key is
        a column, and a column that spells the table's old name is still that column."""
        existing = _replay(
            monkeypatch,
            (_sweep('shop_riff', 'shop_band', 'shop_band'), []),
            ('', [_rename('shop_band', 'shop_band2')]),
        )

        assert list(existing.soft_delete_owned_sweep) == [('shop_riff', 'shop_band2', 'shop_band')]


class TestAReplacedFileBesideAnUnexpandedSquash:
    def test_is_its_squashs_node_and_ordered_before_it(self):
        loader = _Loader(
            ('anc', [[_create()], [_create('other')], [_create('third')]]),
            squash=('anc', ['0001', '0002'], [_create(), _create('other')]),
        )
        del loader.disk_migrations['anc', '0002']

        units, aliases = scanning._replay_units(loader, {('anc', '0001'): ''})

        assert units[0].graph_node == ('anc', '0001_squashed_0002')
        assert scanning._orders(
            ('anc', '0001'), ('anc', '0001_squashed_0002'), loader.graph, aliases
        )


def test_retirements_of_one_key_are_settled_in_the_order_they_were_replayed(monkeypatch):
    """The replay's order, not the filename's: a later migration of an app can sort first."""
    retired = (
        HEADER_SOFT_DELETE_RELATED_RETIRED.format(related_table='shop_a', table='shop_owner')
        + ' [SQL:dddd]\n'
    )
    existing = _replay(
        monkeypatch,
        (_related('shop_a', 'shop_owner'), []),
        (retired, []),
        (retired, []),
        stems=['0001_created', '0009_retired_later', '0002_retired_earlier'],
    )

    assert [site.migration for site in existing.cascade_retirement_sites] == [
        '0009_retired_later',
        '0002_retired_earlier',
    ]


def test_creates_merged_at_a_rename_stay_in_replay_order(monkeypatch):
    """A create filed under the freed name is older than one filed under the new name, if it was
    written first, whichever name it was filed under: the last of the list is the newest."""
    existing = _replay(
        monkeypatch,
        (_related('shop_k', 'shop_old'), []),
        (_related('shop_k', 'shop_x'), []),
        ('', [_drop('shop_x')]),
        ('', [_rename('shop_old', 'shop_x')]),
    )

    assert existing.soft_delete_related_dependencies[('shop_k', 'shop_x', None)] == [
        ('testapp', '0000_auto_enforcement'),
        ('testapp', '0001_auto_enforcement'),
    ]


def test_a_file_the_loader_would_not_load_is_not_read(tmp_path):
    migrations = tmp_path / 'migrations'
    migrations.mkdir()
    for name in ('0001_a.py', '_draft.py', '~backup.py', '__init__.py'):
        (migrations / name).write_text('')

    files = list(_generator.iter_migration_files(types.SimpleNamespace(path=str(tmp_path))))

    assert [path.name for path, _content in files] == ['0001_a.py']
