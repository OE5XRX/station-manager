from django.db import migrations

from apps.rollouts.tag_migration import migrate_tags_to_groups


class Migration(migrations.Migration):
    dependencies = [
        ("stations", "0028_rolloutgroup"),
        ("rollouts", "0004_entry_group"),
        ("deployments", "0004_target_group"),
        ("module_firmware", "0014_target_group"),
    ]

    operations = [
        # Reverse is a no-op: the schema reverse drops the group columns,
        # and tags were never modified, so there is nothing to restore.
        migrations.RunPython(migrate_tags_to_groups, migrations.RunPython.noop),
    ]
