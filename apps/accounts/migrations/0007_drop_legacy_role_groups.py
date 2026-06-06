"""Delete the legacy Django role-groups (admin/operator/member).

After PR-1 (User.is_admin reads membership_level + 13-call-site
refactor + conftest fixtures stopped dual-writing groups), the groups
themselves serve no further purpose. They are removed here.

Reverse re-creates the groups via idempotent get_or_create so that a
rollback within the 30-day backup window leaves the database
recoverable. The reversed groups are EMPTY — membership reconstruction
is not attempted (cardinal: rollback inside the backup window relies
on the pre-cutover backup itself, not on reverse-migration round-trip
fidelity).
"""

from django.db import migrations

LEGACY_GROUPS = ("admin", "operator", "member")


def drop_legacy_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=LEGACY_GROUPS).delete()


def recreate_legacy_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in LEGACY_GROUPS:
        Group.objects.get_or_create(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_add_account_audit_log"),
    ]

    operations = [
        migrations.RunPython(
            drop_legacy_groups,
            reverse_code=recreate_legacy_groups,
        ),
    ]
