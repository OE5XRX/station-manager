from django.db import migrations


class Migration(migrations.Migration):
    """Drop the deprecated User.role column.

    Nothing reads or writes it any more — T5 swept every call site to
    Group-backed properties, T4 populated the Groups from the role
    values, and forms/managers now write directly into user.groups.

    Reverse: re-adds the column with its original choices/default but
    NO data — operators rolling back past this migration must also
    roll back 0002 to repopulate role from the Group memberships.
    """

    dependencies = [("accounts", "0002_role_to_groups")]

    operations = [
        migrations.RemoveField(model_name="user", name="role"),
    ]
