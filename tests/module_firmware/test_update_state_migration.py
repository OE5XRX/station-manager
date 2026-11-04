import pytest

PRE = ("module_firmware", "0016_alter_modulefirmwaretarget_canary_group_and_more")
TESTED = ("module_firmware", "0018_failed_transient_rows")


@pytest.mark.django_db(transaction=True)
def test_rows_at_limit_become_failed(migrator):
    apps = migrator.apply_initial_migration([PRE]).apps
    mt_model = apps.get_model("module_firmware", "ModuleType")
    module_model = apps.get_model("module_firmware", "Module")
    rel_model = apps.get_model("module_firmware", "ModuleFirmwareRelease")
    cs_model = apps.get_model("module_firmware", "ModuleFirmwareConvergenceState")
    mt = mt_model.objects.create(key="fm", display_name="FM")

    def rel(v):
        return rel_model.objects.create(
            module_type=mt,
            version=v,
            storage_key="k",
            sha256="a" * 64,
            size_bytes=1,
            source_repo="o/r",
            source_tag=v,
        )

    def row(uid, state, tf, conv):
        m = module_model.objects.create(uid=uid, module_type=mt, firmware_convergence=conv)
        return cs_model.objects.create(
            module=m, target_release=rel(uid), state=state, transient_failures=tf
        )

    a = row("a", "updating", 5, "updating")
    b = row("b", "updating", 4, "updating")
    c = row("c", "quarantined", 7, "quarantined")

    new = migrator.apply_tested_migration([TESTED]).apps
    cs_after = new.get_model("module_firmware", "ModuleFirmwareConvergenceState")
    module_after = new.get_model("module_firmware", "Module")
    ra, rb, rc = (cs_after.objects.get(pk=x.pk) for x in (a, b, c))
    assert (ra.state, ra.reason_code) == ("failed", "transient_limit")
    assert rb.state == "updating" and rb.reason_code == ""
    assert rc.state == "quarantined"
    assert module_after.objects.get(pk=a.module_id).firmware_convergence == "failed"
    assert module_after.objects.get(pk=b.module_id).firmware_convergence == "updating"
    assert module_after.objects.get(pk=c.module_id).firmware_convergence == "quarantined"
