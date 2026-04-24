"""One-shot data migration: reconcile DeploymentResults left in a
non-terminal status when their parent Deployment was cancelled or
failed before this fix landed.

The cancel view used to flip only PENDING children. A child that was
already DOWNLOADING / INSTALLING / REBOOTING / VERIFYING stayed at
that status forever. Because supersession.active_statuses includes
those, every future upgrade for the affected station blew up with
ActiveDeploymentConflictError.
"""

from django.db import migrations
from django.utils import timezone

NON_TERMINAL = ("pending", "downloading", "installing", "rebooting", "verifying")
TERMINAL_PARENT_STATES = ("cancelled", "failed")


def reconcile(apps, schema_editor):
    DeploymentResult = apps.get_model("deployments", "DeploymentResult")
    now = timezone.now()
    DeploymentResult.objects.filter(
        deployment__status__in=TERMINAL_PARENT_STATES,
        status__in=NON_TERMINAL,
    ).update(
        status="cancelled",
        completed_at=now,
        error_message="Parent deployment was already terminal; reconciled by data migration.",
    )


def noop_reverse(apps, schema_editor):
    # Forward-only — we cannot reconstruct each result's pre-fix
    # status without an audit trail, and the prior state was the
    # bug we're undoing.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("deployments", "0002_swap_to_image_release"),
    ]

    operations = [
        migrations.RunPython(reconcile, noop_reverse),
    ]
