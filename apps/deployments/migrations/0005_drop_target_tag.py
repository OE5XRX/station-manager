from django.db import migrations, models


def leftover_tag_rows_to_group(apps, schema_editor):
    """Safety net: rollouts.0005 already remapped tag rows; never leave 'tag' behind."""
    deployment = apps.get_model("deployments", "Deployment")
    deployment.objects.filter(target_type="tag").update(target_type="group")


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0004_target_group"),
        ("rollouts", "0005_tags_to_groups"),
    ]

    operations = [
        migrations.RunPython(leftover_tag_rows_to_group, migrations.RunPython.noop),
        migrations.RemoveField(model_name="deployment", name="target_tag"),
        migrations.AlterField(
            model_name="deployment",
            name="target_type",
            field=models.CharField(
                choices=[
                    ("group", "By Group"),
                    ("station", "Single Station"),
                    ("all", "All Stations"),
                ],
                max_length=10,
                verbose_name="target type",
            ),
        ),
    ]
