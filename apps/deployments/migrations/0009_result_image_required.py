import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0008_backfill_version_fields"),
    ]

    operations = [
        migrations.AlterField(
            model_name="deploymentresult",
            name="image_release",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deployment_results",
                to="images.imagerelease",
                verbose_name="image release",
            ),
        ),
        migrations.RemoveField(
            model_name="deployment",
            name="image_release",
        ),
        # Irreversible: Deployment.image_release is gone and results of
        # multi-machine deployments carry different images, so there is no
        # single value to restore. Reverse runs operations last-to-first, so
        # this raises IrreversibleError before any schema change is undone
        # (instead of a raw DB error re-adding the NOT NULL column).
        migrations.RunPython(migrations.RunPython.noop),
    ]
