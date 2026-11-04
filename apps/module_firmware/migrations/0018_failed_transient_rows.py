from django.db import migrations

# Hardcoded on purpose: the migration must keep its meaning even if the live
# DRAIN_TRANSIENT_LIMIT constant changes later.
_LIMIT = 5


def forward(apps, schema_editor):
    CS = apps.get_model("module_firmware", "ModuleFirmwareConvergenceState")
    Module = apps.get_model("module_firmware", "Module")
    rows = CS.objects.filter(state="updating", transient_failures__gte=_LIMIT)
    module_ids = list(rows.values_list("module_id", flat=True))
    rows.update(state="failed", reason_code="transient_limit")
    Module.objects.filter(pk__in=module_ids, firmware_convergence="updating").update(
        firmware_convergence="failed"
    )


class Migration(migrations.Migration):
    dependencies = [("module_firmware", "0017_update_state")]

    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
