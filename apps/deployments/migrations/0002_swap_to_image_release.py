# NOTE: This already-applied migration was deliberately edited after the fact in
# tandem with 0001_initial: the ``RemoveField(deployment.firmware_artifact)``
# operation was dropped because the field is no longer created in 0001 (the
# ``firmware`` app was deleted). Safe because Django re-runs migrations by NAME,
# not content — this never re-applies on an existing DB, and a fresh DB reaches
# the identical end state. See the NOTE in 0001_initial.py for the full rationale.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("deployments", "0001_initial"),
        ("images", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="deployment",
            name="image_release",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="deployments",
                to="images.imagerelease",
                verbose_name="image release",
            ),
        ),
        migrations.AlterField(
            model_name="deploymentresult",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("downloading", "Downloading"),
                    ("installing", "Installing"),
                    ("rebooting", "Rebooting"),
                    ("verifying", "Verifying"),
                    ("success", "Success"),
                    ("failed", "Failed"),
                    ("rolled_back", "Rolled Back"),
                    ("cancelled", "Cancelled"),
                    ("superseded", "Superseded"),
                ],
                default="pending",
                max_length=16,
                verbose_name="status",
            ),
        ),
    ]
