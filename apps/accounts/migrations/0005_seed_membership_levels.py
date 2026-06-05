"""Seed User.membership_level from existing Group memberships.

Mapping (highest precedence wins):
  - group 'admin'    -> ADMIN
  - group 'operator' -> STAFF       (rename: operator collides with amateur-radio meaning)
  - group 'member'   -> MEMBER
  - none of the above -> APPLICANT  (= default from 0004)

Reverse is a noop. Re-running forward is idempotent because the mapping
overrides the APPLICANT default; once set, it stays. The legacy Django
groups are NOT touched here — migration 0009 deletes them after the
call-site refactor has landed.
"""

from django.db import migrations

LEVEL_PRECEDENCE = [
    ("admin", "admin"),
    ("operator", "staff"),
    ("member", "member"),
]


def seed_membership_levels(apps, schema_editor):
    User = apps.get_model("accounts", "User")

    for user in User.objects.all().order_by("pk"):
        user_group_names = set(user.groups.values_list("name", flat=True))
        for group_name, level in LEVEL_PRECEDENCE:
            if group_name in user_group_names:
                user.membership_level = level
                user.save(update_fields=["membership_level"])
                break
        # No group match -> APPLICANT default from 0004 stays


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_add_membership_level"),
    ]

    operations = [
        migrations.RunPython(
            seed_membership_levels,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
