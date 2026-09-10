# Hand-written, reproducing what a generator through 2.9.0 wrote when it attributed a cascade
# rule to the app that walked the *related* model rather than the owner table's app. The rule
# fires on ``crossapp_retire_owner_retiree``, so 2.10.0 retires it from that app -- and nothing
# ordered that drop after this file until the retirement started carrying an edge. Issue #49.
# [DIGEST:1c49a0f5e0d1]

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('crossapp_retire_child', '0001_initial'),
        ('crossapp_retire_owner', '0001_initial'),
    ]

    operations = [
        # Soft Delete Related Rule on "crossapp_retire_child_dependant" that is related to "crossapp_retire_owner_retiree"! [SQL:0f3a1b2c4d5e]
        migrations.RunSQL(
            sql="""
    CREATE OR REPLACE RULE soft_delete_related_crossapp_retire_child_dependant
        AS ON UPDATE TO "crossapp_retire_owner_retiree"
        WHERE old._deleted_at IS NULL AND new._deleted_at IS NOT NULL AND
              COALESCE(current_setting('rules.hard_deletion', true), '') <> 'on'
        DO ALSO (
            UPDATE "crossapp_retire_child_dependant"
            SET _deleted_at = NOW()
            WHERE "owner_id" = old."id"
        );
""",
            reverse_sql='DROP RULE soft_delete_related_crossapp_retire_child_dependant ON "crossapp_retire_owner_retiree";',
        ),
    ]
