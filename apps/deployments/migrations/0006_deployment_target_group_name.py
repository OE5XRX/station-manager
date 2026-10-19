from django.db import migrations, models


def backfill_group_names(apps, schema_editor):
    # Existing group deployments (incl. ones mapped from tags by rollouts 0005)
    # snapshot their group's current name; group-less rows stay "(deleted)".
    Deployment = apps.get_model("deployments", "Deployment")
    for dep in Deployment.objects.filter(target_group__isnull=False).select_related(
        "target_group"
    ):
        dep.target_group_name = dep.target_group.name
        dep.save(update_fields=["target_group_name"])


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0005_drop_target_tag"),
    ]

    operations = [
        migrations.AddField(
            model_name="deployment",
            name="target_group_name",
            field=models.CharField(blank=True, max_length=100, verbose_name="target group name"),
        ),
        migrations.RunPython(backfill_group_names, migrations.RunPython.noop),
    ]
