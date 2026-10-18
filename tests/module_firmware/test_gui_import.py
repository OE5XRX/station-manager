from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.images.github_releases import GitHubRelease
from apps.module_firmware.models import (
    Module,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleType,
)

User = get_user_model()

GH_FETCH = "apps.module_firmware.views.github_releases.fetch_releases"


@pytest.fixture
def staff(db):
    return User.objects.create_user(username="s", password="x", membership_level="staff")


@pytest.fixture
def viewer(db):
    return User.objects.create_user(username="v", password="x")


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(
        key="fm",
        display_name="FM",
        firmware_repo="OE5XRX/FW-RemoteStation",
        release_asset_prefix="fm-sa818",
    )


def _gh_release(tag, *signed):
    names = set()
    for s in signed:
        names.add(s)
        names.add(s + ".bundle")
    return GitHubRelease(tag=tag, html_url="", is_latest=False, asset_names=frozenset(names))


def _config_logs():
    return AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)


# ---------------------------------------------------------------------------
# Setup hints
# ---------------------------------------------------------------------------


def test_release_list_setup_hint_no_types(client, staff):
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:release_list"))
    assert resp.status_code == 200
    assert resp.context["all_types_count"] == 0
    body = resp.content.decode()
    assert "No module type configured" in body
    assert reverse("module_firmware:type_create") in body


def test_release_list_setup_hint_no_cta_for_viewer(client, viewer):
    client.force_login(viewer)
    resp = client.get(reverse("module_firmware:release_list"))
    body = resp.content.decode()
    assert "No module type configured" in body
    assert reverse("module_firmware:type_create") not in body


def test_release_list_lists_incomplete_types(client, staff, fm):
    no_prefix = ModuleType.objects.create(
        key="pwr", display_name="Power", firmware_repo="OE5XRX/HW-PowerBoard"
    )
    no_repo = ModuleType.objects.create(
        key="dbg", display_name="Debug", release_asset_prefix="dbg"
    )
    untrusted = ModuleType.objects.create(
        key="evil",
        display_name="Evil",
        firmware_repo="someone/FW",
        release_asset_prefix="evil",
    )
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:release_list"))
    assert resp.context["all_types_count"] == 4
    incomplete = {e["type"].pk: e["missing"] for e in resp.context["incomplete_types"]}
    assert set(incomplete) == {no_prefix.pk, no_repo.pk, untrusted.pk}
    assert any("prefix" in m for m in incomplete[no_prefix.pk])
    assert any("repo" in m for m in incomplete[no_repo.pk])
    assert incomplete[untrusted.pk] == ["untrusted repo owner"]
    body = resp.content.decode()
    for t in (no_prefix, no_repo, untrusted):
        assert reverse("module_firmware:type_update", args=[t.pk]) in body
    assert "untrusted repo owner" in body
    # Untrusted type is not offered for import.
    assert untrusted.pk not in {t.pk for t in resp.context["module_types"]}
    assert fm.pk in {t.pk for t in resp.context["module_types"]}


def test_release_list_no_hint_when_all_complete(client, staff, fm):
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:release_list"))
    body = resp.content.decode()
    assert resp.context["incomplete_types"] == []
    assert "No module type configured" not in body


# ---------------------------------------------------------------------------
# GitHub variant preview
# ---------------------------------------------------------------------------


def test_gh_partial_shows_variants(client, staff, fm):
    rel = _gh_release("26.07.04-01", "fm-sa818-vhf.signed.bin", "fm-sa818-uhf.signed.bin")
    client.force_login(staff)
    with mock.patch(GH_FETCH, return_value=[rel]):
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={fm.pk}")
    assert resp.status_code == 200
    row = resp.context["rows"][0]
    assert row["variants"] == ["uhf", "vhf"]
    assert row["state"] == "ready"
    body = resp.content.decode()
    assert "vhf" in body and "uhf" in body


def test_gh_partial_bandless_variant_label(client, staff, fm):
    rel = _gh_release("1.0", "fm-sa818.signed.bin")
    client.force_login(staff)
    with mock.patch(GH_FETCH, return_value=[rel]):
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={fm.pk}")
    assert resp.context["rows"][0]["variants"] == [""]
    assert "(bandless)" in resp.content.decode()


def test_gh_partial_no_assets_state(client, staff, fm):
    rel = _gh_release("26.07.04-01", "other-vhf.signed.bin")
    client.force_login(staff)
    with mock.patch(GH_FETCH, return_value=[rel]):
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={fm.pk}")
    assert resp.context["rows"][0]["state"] == "no_assets"
    body = resp.content.decode()
    assert "fm-sa818-&lt;variant&gt;.signed.bin + .bundle" in body
    assert 'name="tag"' not in body


def test_gh_partial_no_assets_beats_queued(client, staff, fm):
    ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="t1", requested_by=staff
    )
    rel = _gh_release("t1", "other-vhf.signed.bin")
    client.force_login(staff)
    with mock.patch(GH_FETCH, return_value=[rel]):
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={fm.pk}")
    assert resp.context["rows"][0]["state"] == "no_assets"


def test_gh_partial_missing_variant_warning(client, staff, fm):
    Module.objects.create(uid="m1", module_type=fm, variant="uhf")
    Module.objects.create(uid="m2", module_type=fm, variant="vhf")
    Module.objects.create(uid="m3", module_type=fm, variant="")
    rel = _gh_release("26.07.04-01", "fm-sa818-vhf.signed.bin")
    client.force_login(staff)
    with mock.patch(GH_FETCH, return_value=[rel]):
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={fm.pk}")
    row = resp.context["rows"][0]
    assert row["missing_variants"] == ["uhf"]
    body = resp.content.decode()
    assert "uhf" in body


def test_gh_partial_untrusted_type_errors(client, staff):
    t = ModuleType.objects.create(
        key="evil", display_name="Evil", firmware_repo="someone/FW", release_asset_prefix="e"
    )
    client.force_login(staff)
    with mock.patch(GH_FETCH) as fetch:
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={t.pk}")
    assert resp.status_code == 200
    assert resp.context["error"]
    fetch.assert_not_called()


# ---------------------------------------------------------------------------
# Live import-job status
# ---------------------------------------------------------------------------


def test_import_jobs_partial_polls_only_when_active(client, viewer, fm, staff):
    url = reverse("module_firmware:import_jobs")
    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm,
        source_repo=fm.firmware_repo,
        tag="t1",
        status=ModuleFirmwareImportJob.Status.READY,
        requested_by=staff,
    )
    client.force_login(viewer)
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.context["has_active"] is False
    body = resp.content.decode()
    assert 'hx-trigger="every 5s"' not in body
    assert "t1" in body

    job.status = ModuleFirmwareImportJob.Status.RUNNING
    job.save()
    resp = client.get(url)
    assert resp.context["has_active"] is True
    body = resp.content.decode()
    assert 'hx-trigger="every 5s"' in body
    assert f'hx-get="{url}"' in body
    assert 'hx-swap="outerHTML"' in body


def test_import_jobs_partial_limits_to_20_and_shows_release_version(client, viewer, fm, staff):
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="26.07.04-77",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="26.07.04-77",
    )
    for i in range(25):
        ModuleFirmwareImportJob.objects.create(
            module_type=fm,
            source_repo=fm.firmware_repo,
            tag=f"tag-{i}",
            status=ModuleFirmwareImportJob.Status.READY,
            release=rel if i == 24 else None,
        )
    client.force_login(viewer)
    resp = client.get(reverse("module_firmware:import_jobs"))
    assert len(resp.context["jobs"]) == 20
    assert "26.07.04-77" in resp.content.decode()


def test_release_list_includes_job_partial(client, viewer, fm):
    ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="pending-tag"
    )
    client.force_login(viewer)
    resp = client.get(reverse("module_firmware:release_list"))
    body = resp.content.decode()
    assert "pending-tag" in body
    assert 'hx-trigger="every 5s"' in body


def test_import_jobs_partial_requires_login(client):
    resp = client.get(reverse("module_firmware:import_jobs"))
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Feedback + audit
# ---------------------------------------------------------------------------


def _messages(resp):
    return [str(m) for m in resp.context["messages"]]


def test_import_writes_audit_and_message(client, staff, fm):
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"), {"module_type": fm.pk, "tag": "v9"}, follow=True
    )
    assert "Import of v9 queued." in _messages(resp)
    log = _config_logs().get()
    assert log.details["object"] == "module_firmware_import"
    assert log.details["action"] == "queued"
    assert log.details["module_type"] == "fm"
    assert log.details["tag"] == "v9"

    # Second POST while pending → info, no extra audit.
    resp = client.post(
        reverse("module_firmware:import"), {"module_type": fm.pk, "tag": "v9"}, follow=True
    )
    assert any("already in progress" in m for m in _messages(resp))
    assert _config_logs().count() == 1


def test_import_not_ready_type_error_message(client, staff):
    t = ModuleType.objects.create(key="bare", display_name="Bare", firmware_repo="OE5XRX/x")
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"), {"module_type": t.pk, "tag": "v1"}, follow=True
    )
    msgs = resp.context["messages"]
    assert any(m.level_tag == "error" for m in msgs)
    assert not ModuleFirmwareImportJob.objects.exists()
    assert _config_logs().count() == 0


def test_import_untrusted_type_rejected(client, staff):
    t = ModuleType.objects.create(
        key="evil", display_name="Evil", firmware_repo="someone/FW", release_asset_prefix="e"
    )
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"), {"module_type": t.pk, "tag": "v1"}, follow=True
    )
    assert any(m.level_tag == "error" for m in resp.context["messages"])
    assert not ModuleFirmwareImportJob.objects.exists()


def test_archive_restore_audited_once(client, staff, fm):
    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="1.0",
        storage_key="k",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="1.0",
    )
    client.force_login(staff)
    archive = reverse("module_firmware:archive", args=[rel.pk])
    restore = reverse("module_firmware:restore", args=[rel.pk])

    resp = client.post(archive, follow=True)
    assert any(m.level_tag == "success" for m in resp.context["messages"])
    client.post(archive)
    logs = _config_logs()
    assert logs.count() == 1
    log = logs.get()
    assert log.details["object"] == "module_firmware_release"
    assert log.details["action"] == "archived"
    assert log.details["module_type"] == "fm"
    assert log.details["variant"] == "vhf"
    assert log.details["version"] == "1.0"

    client.post(restore)
    client.post(restore)
    assert _config_logs().count() == 2
    assert _config_logs().filter(details__action="restored").count() == 1
    rel.refresh_from_db()
    assert rel.archived_at is None


def test_release_list_slashless_repo_is_blocked(client, staff, fm):
    bad = ModuleType.objects.create(
        key="bad", display_name="Bad", firmware_repo="OE5XRX", release_asset_prefix="b"
    )
    client.force_login(staff)
    resp = client.get(reverse("module_firmware:release_list"))
    incomplete = {e["type"].pk: e["missing"] for e in resp.context["incomplete_types"]}
    assert bad.pk in incomplete
    assert incomplete[bad.pk]
    assert bad.pk not in {t.pk for t in resp.context["module_types"]}
    assert fm.pk in {t.pk for t in resp.context["module_types"]}


def test_is_trusted_repo_requires_owner_repo_shape(settings):
    from apps.module_firmware.trust import is_trusted_repo

    settings.MODULE_FIRMWARE_TRUSTED_REPO_OWNERS = ["OE5XRX"]
    assert is_trusted_repo("OE5XRX/FW")
    # Finding 5: case-sensitive, matching the cosign identity regex.
    for bad in ("OE5XRX", "OE5XRX/", "/FW", "OE5XRX/a/b", "", None, "someone/FW", "oe5xrx/FW"):
        assert not is_trusted_repo(bad), bad


def test_gh_partial_non_integer_module_type(client, staff, db):
    client.force_login(staff)
    with mock.patch(GH_FETCH) as fetch:
        resp = client.get(reverse("module_firmware:gh_partial") + "?module_type=abc")
    assert resp.status_code == 200
    assert resp.context["error"]
    fetch.assert_not_called()


def test_import_non_integer_module_type(client, staff, db):
    client.force_login(staff)
    resp = client.post(reverse("module_firmware:import"), {"module_type": "abc", "tag": "v1"})
    assert resp.status_code == 302
    resp = client.get(resp.url)
    assert any(m.level_tag == "error" for m in resp.context["messages"])
    assert not ModuleFirmwareImportJob.objects.exists()


def test_archive_desired_release_reconciles_rollup(client, staff, fm, station_factory):
    from apps.module_firmware.models import (
        ModuleAssignmentHistory,
        ModuleFirmwareConvergenceState,
        ModuleFirmwareTarget,
    )

    rel = ModuleFirmwareRelease.objects.create(
        module_type=fm,
        variant="vhf",
        version="2.0",
        storage_key="k2",
        sha256="a" * 64,
        size_bytes=1,
        source_repo="r",
        source_tag="2.0",
    )
    ModuleFirmwareTarget.objects.create(module_type=fm, version="2.0")
    st = station_factory()
    m = Module.objects.create(
        uid="ARC",
        module_type=fm,
        variant="vhf",
        last_reported_version="1.0",
        firmware_convergence=Module.Convergence.QUARANTINED,
    )
    ModuleAssignmentHistory.objects.create(module=m, station=st, slot="slot0")
    ModuleFirmwareConvergenceState.objects.create(
        module=m, target_release=rel, state=ModuleFirmwareConvergenceState.State.QUARANTINED
    )
    client.force_login(staff)
    client.post(reverse("module_firmware:archive", args=[rel.pk]))
    m.refresh_from_db()
    # Desired release gone: drift stays visible, but no longer quarantined on it.
    assert m.firmware_convergence == Module.Convergence.UPDATING
    client.post(reverse("module_firmware:restore", args=[rel.pk]))
    m.refresh_from_db()
    assert m.firmware_convergence == Module.Convergence.QUARANTINED


# ---------------------------------------------------------------------------
# Review-loop round 1: tag validation (finding 3) + Unicode digits (finding 4)
# ---------------------------------------------------------------------------


def test_is_valid_tag():
    from apps.module_firmware.trust import is_valid_tag

    for good in ("v1", "26.09.15-01", "a" * 64, "fw_1.2-rc3"):
        assert is_valid_tag(good), good
    for bad in ("", None, "a" * 65, "../x", "v1..2", "-v1", ".v1", "v1/x", "v1 x", "v1\n", "v²"):
        assert not is_valid_tag(bad), bad


@pytest.mark.parametrize("tag", ["a" * 65, "../evil", "v1..2", "v1/x", "v1?x=1"])
def test_import_rejects_invalid_tag(client, staff, fm, tag):
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"), {"module_type": fm.pk, "tag": tag}, follow=True
    )
    assert any(m.level_tag == "error" for m in resp.context["messages"])
    assert not ModuleFirmwareImportJob.objects.exists()
    assert _config_logs().count() == 0


def test_import_release_tag_rejects_invalid_tag_without_fetch(fm):
    from apps.module_firmware import releases

    job = ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag="../evil"
    )
    with mock.patch("apps.module_firmware.releases.fetch_release_by_tag") as fetch:
        releases.import_release_tag(job)
    fetch.assert_not_called()
    job.refresh_from_db()
    assert job.status == ModuleFirmwareImportJob.Status.FAILED
    assert "tag" in job.error_message


def test_import_unicode_digit_module_type(client, staff, fm):
    client.force_login(staff)
    resp = client.post(
        reverse("module_firmware:import"), {"module_type": "²", "tag": "v1"}, follow=True
    )
    assert resp.status_code == 200
    assert any(m.level_tag == "error" for m in resp.context["messages"])
    assert not ModuleFirmwareImportJob.objects.exists()


def test_gh_partial_unicode_digit_module_type(client, staff, db):
    client.force_login(staff)
    with mock.patch(GH_FETCH) as fetch:
        resp = client.get(reverse("module_firmware:gh_partial"), {"module_type": "²"})
    assert resp.status_code == 200
    assert resp.context["error"]
    fetch.assert_not_called()


def test_module_list_unicode_digit_station_filter(client, viewer, db):
    client.force_login(viewer)
    resp = client.get(reverse("module_firmware:module_list"), {"station": "²"})
    assert resp.status_code == 200


@pytest.mark.parametrize("tag", ["v1.0+build", "release/1.0"])
def test_gh_partial_invalid_tag_not_importable(client, staff, fm, tag):
    # A tag is_valid_tag refuses must not be offered for import (the POST
    # would only answer "Invalid release tag") — invalid_tag beats every state.
    ModuleFirmwareImportJob.objects.create(
        module_type=fm, source_repo=fm.firmware_repo, tag=tag, requested_by=staff
    )
    rel = _gh_release(tag, "fm-sa818-vhf.signed.bin")
    client.force_login(staff)
    with mock.patch(GH_FETCH, return_value=[rel]):
        resp = client.get(reverse("module_firmware:gh_partial") + f"?module_type={fm.pk}")
    assert resp.context["rows"][0]["state"] == "invalid_tag"
    body = resp.content.decode()
    assert 'name="tag"' not in body
    assert "max 64 characters" in body
