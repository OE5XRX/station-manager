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
    for user in User.objects.all():
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
        ("auth", "__latest__"),
    ]

    operations = [
        migrations.RunPython(
            create_groups_and_assign,
            reverse_remove_users_from_groups,
        ),
    ]
