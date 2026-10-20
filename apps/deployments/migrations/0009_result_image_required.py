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
    ]
