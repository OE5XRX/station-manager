from django.db import migrations
from django.db.models import OuterRef, Subquery, Value
from django.db.models.functions import Coalesce


def backfill(apps, schema_editor):
    """Copy the deployment's single image onto the new version fields.

    Every pre-existing Deployment has a non-null ``image_release`` (PROTECT,
    NOT NULL), so the backfill is total: the deployment gets its tag/channel
    snapshot and every result gets the deployment's image. Set-based UPDATEs
    with correlated subqueries — no per-row loop, never aborts.
    """
    Deployment = apps.get_model("deployments", "Deployment")
    DeploymentResult = apps.get_model("deployments", "DeploymentResult")
    ImageRelease = apps.get_model("images", "ImageRelease")

    release = ImageRelease._base_manager.filter(pk=OuterRef("image_release_id"))
    Deployment.objects.update(
        # Coalesce: the FK is NOT NULL so the subquery always hits, but a
        # NULL here would abort on the NOT NULL columns — never risk that.
        image_tag=Coalesce(Subquery(release.values("tag")[:1]), Value("")),
        image_channel=Coalesce(Subquery(release.values("channel")[:1]), Value("release")),
    )
    DeploymentResult.objects.filter(image_release__isnull=True).update(
        image_release_id=Subquery(
            Deployment.objects.filter(pk=OuterRef("deployment_id")).values("image_release_id")[:1]
        )
    )


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0007_version_based_schema"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
