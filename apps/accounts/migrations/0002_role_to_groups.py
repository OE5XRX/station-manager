"""Map User.role values to Django auth.Group memberships.

Forward
-------
Idempotently creates the three default groups (admin, operator,
member) and assigns each existing User to the group matching their
.role value. The User.role column stays in place — Task 5 sweeps
call sites onto Group-backed properties, Task 6 drops the column.

Reverse — DATA-LOSS WARNING
---------------------------
group.user_set.clear() removes ALL users from the three groups,
including any added by hand via Django Admin between the forward
run and the reverse. The M2M through-row carries no provenance, so
we cannot distinguish "added by this migration" from "added by an
operator later." Roll-back recovery: until Task 6 runs, the User.role
column still holds the original mapping and can be replayed via the
forward callable.

The Group rows themselves are preserved (an admin may have created
unrelated members on them). Same reasoning.
"""

from django.db import migrations

GROUPS = ("admin", "operator", "member")


def create_groups_and_assign(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("accounts", "User")

    # Idempotent group creation.
    groups_by_name = {}
    for name in GROUPS:
        group, _ = Group.objects.get_or_create(name=name)
        groups_by_name[name] = group

    # Assign every existing user to the group matching their .role
    # value. Users with an unrecognized role (shouldn't happen given
    # TextChoices, but defense-in-depth) get no group.
    # order_by('pk') gives deterministic replay on partial failure.
    for user in User.objects.all().order_by("pk"):
        target = groups_by_name.get(user.role)
        if target is not None:
            user.groups.add(target)


def reverse_remove_users_from_groups(apps, schema_editor):
    """Pull users back out of the three groups; leave the groups themselves
    alone (they might have admin-defined members we don't know about).
    """
    Group = apps.get_model("auth", "Group")
    for name in GROUPS:
        try:
            group = Group.objects.get(name=name)
        except Group.DoesNotExist:
            continue
        group.user_set.clear()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        # Pin to whatever auth ships with our Django line; we only
        # need Group's historical schema, not a specific auth migration.
        ("auth", "__latest__"),
    ]

    operations = [
        migrations.RunPython(
            create_groups_and_assign,
            reverse_remove_users_from_groups,
        ),
    ]
