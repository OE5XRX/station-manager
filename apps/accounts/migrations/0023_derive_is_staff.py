from django.db import migrations


def derive(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    User.objects.update(is_staff=False, is_superuser=False)
    User.objects.filter(membership_level="admin").update(is_staff=True, is_superuser=True)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0022_accountauditlog_details_permission_denied")]
    operations = [migrations.RunPython(derive, migrations.RunPython.noop)]
