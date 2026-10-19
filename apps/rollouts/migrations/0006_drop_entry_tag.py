import django.db.models.deletion
from django.db import migrations, models


def drop_entries_without_group(apps, schema_editor):
    """Delete entries 0005 could not map to a group, then close the gaps.

    0005 maps every entry's tag to a group, so this is a safety net that keeps
    the NOT NULL alteration below from ever aborting the migration. Historical
    models don't fire the runtime post_delete renumber signal, so renumber here.
    """
    entry_model = apps.get_model("rollouts", "RolloutSequenceEntry")
    orphans = entry_model.objects.filter(group__isnull=True)
    sequence_ids = set(orphans.values_list("sequence_id", flat=True))
    orphans.delete()
    for sequence_id in sequence_ids:
        entries = entry_model.objects.filter(sequence_id=sequence_id).order_by("position")
        for idx, entry in enumerate(entries):
            if entry.position != idx:
                entry.position = idx
                entry.save(update_fields=["position"])


class Migration(migrations.Migration):
    dependencies = [
        ("rollouts", "0005_tags_to_groups"),
        ("stations", "0028_rolloutgroup"),
    ]

    operations = [
        migrations.RunPython(drop_entries_without_group, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="rolloutsequenceentry",
            name="uniq_tag_per_sequence",
        ),
        migrations.RemoveField(
            model_name="rolloutsequenceentry",
            name="tag",
        ),
        migrations.AlterField(
            model_name="rolloutsequenceentry",
            name="group",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="rollout_entries",
                to="stations.rolloutgroup",
            ),
        ),
        migrations.AddConstraint(
            model_name="rolloutsequenceentry",
            constraint=models.UniqueConstraint(
                fields=("sequence", "group"), name="uniq_group_per_sequence"
            ),
        ),
    ]
