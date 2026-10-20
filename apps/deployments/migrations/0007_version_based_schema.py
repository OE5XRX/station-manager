import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0006_deployment_target_group_name"),
        ("images", "0008_imageimportjob_channel"),
    ]

    operations = [
        migrations.AddField(
            model_name="deployment",
            name="image_tag",
            field=models.CharField(default="", max_length=64, verbose_name="image tag"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="deployment",
            name="image_channel",
            field=models.CharField(default="release", max_length=32, verbose_name="image channel"),
        ),
        migrations.AddField(
            model_name="deployment",
            name="skipped_stations",
            field=models.JSONField(blank=True, default=list, verbose_name="skipped stations"),
        ),
        migrations.AddField(
            model_name="deploymentresult",
            name="image_release",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deployment_results",
                to="images.imagerelease",
                verbose_name="image release",
            ),
        ),
    ]
