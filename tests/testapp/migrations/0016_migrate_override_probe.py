"""Proves ``src/guitars/management/commands/migrate.py``'s ``tenancy_bypassed()`` wrapper
for real, rather than only via the ``guitars.tenancy.W001`` system check that confirms it's
installed. A migration is operator-invoked, cross-tenant by nature (no tenant scope can be
open while it runs), and ``FORCE ROW LEVEL SECURITY`` means even the owning role is subject
to the policy without an explicit bypass -- so without this wrapper, a backfill spanning
more than one tenant would have every row filtered by the policy and silently affect zero.

The one statement below -- one ``UPDATE`` matching rows under two different labels, issued
with no tenant scope open anywhere -- only touches both because ``migrate`` bypasses
tenancy for its whole run. Raw SQL throughout, not the ORM: a ``RunPython`` migration's
``apps.get_model()`` returns a bare historical reconstruction with none of ``GuitarModel``'s
custom methods, and the point here is the *database*'s enforcement, which raw SQL exercises
directly. Everything created to prove it (two Labels, two Releases) is hard-deleted again
before the migration ends, so this leaves the shared ``testapp_label``/``testapp_release``
tables exactly as it found them -- other tests' row-count assertions against them
(e.g. ``tests/test_tenancy_models.py``) never see it. Only the one-row marker table,
queried by ``tests/test_migrate_override.py``, survives.
"""

from django.db import migrations


def _backfill_across_tenants_then_clean_up(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            'INSERT INTO testapp_label (name, _created_at, _updated_at) '
            'VALUES (%s, NOW(), NOW()) RETURNING id',
            ['Migrate Override Probe A'],
        )
        (label_a_id,) = cursor.fetchone()
        cursor.execute(
            'INSERT INTO testapp_label (name, _created_at, _updated_at) '
            'VALUES (%s, NOW(), NOW()) RETURNING id',
            ['Migrate Override Probe B'],
        )
        (label_b_id,) = cursor.fetchone()

        cursor.execute(
            'INSERT INTO testapp_release (title, label_id, _created_at, _updated_at) '
            'VALUES (%s, %s, NOW(), NOW()) RETURNING id',
            ['probe-a', label_a_id],
        )
        (release_a_id,) = cursor.fetchone()
        cursor.execute(
            'INSERT INTO testapp_release (title, label_id, _created_at, _updated_at) '
            'VALUES (%s, %s, NOW(), NOW()) RETURNING id',
            ['probe-b', label_b_id],
        )
        (release_b_id,) = cursor.fetchone()

        # The point: one statement, spanning both labels, with no tenant scope open
        # anywhere in this process. Only correct because `migrate` bypasses tenancy.
        cursor.execute(
            'UPDATE testapp_release SET title = %s WHERE id IN (%s, %s)',
            ['backfilled', release_a_id, release_b_id],
        )
        affected = cursor.rowcount

        # Hard, not soft: a plain DELETE here would only be rewritten by the soft_delete
        # rule into an UPDATE, leaving the rows (invisible to `.objects`, still present to
        # `_all_objects`/`tenancy_bypassed()`) -- exactly what test_tenancy_models.py's
        # `Release._all_objects.count()` assertions would then trip over.
        cursor.execute("SELECT set_config('rules.hard_deletion', 'on', TRUE)")
        cursor.execute(
            'DELETE FROM testapp_release WHERE id IN (%s, %s)', [release_a_id, release_b_id]
        )
        cursor.execute('DELETE FROM testapp_label WHERE id IN (%s, %s)', [label_a_id, label_b_id])

        cursor.execute(
            'INSERT INTO testapp_migrate_override_probe (affected_count) VALUES (%s)',
            [affected],
        )


class Migration(migrations.Migration):
    dependencies = [
        ('testapp', '0015_auto_enforcement'),
    ]

    operations = [
        migrations.RunSQL(
            sql='CREATE TABLE testapp_migrate_override_probe (affected_count integer NOT NULL);',
            reverse_sql='DROP TABLE testapp_migrate_override_probe;',
        ),
        migrations.RunPython(_backfill_across_tenants_then_clean_up, migrations.RunPython.noop),
    ]
