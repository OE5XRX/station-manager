"""Version-based deployment mask: form choices, create, preview, detail."""

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse

from apps.deployments.forms import DeploymentForm
from apps.deployments.models import Deployment, DeploymentResult
from apps.images.models import ImageRelease
from apps.stations.models import RolloutGroup

QEMU = "qemux86-64"
RPI = "raspberrypi4-64"


def _release(tag, machine, channel="release", ota=True):
    return ImageRelease.objects.create(
        tag=tag,
        machine=machine,
        channel=channel,
        s3_key=f"images/{tag}/{machine}.wic.bz2",
        sha256="a" * 64,
        size_bytes=1000,
        rootfs_s3_key=f"images/{tag}/{machine}.rootfs.bz2" if ota else "",
        rootfs_sha256="b" * 64 if ota else "",
        rootfs_size_bytes=500 if ota else 0,
    )


@pytest.fixture
def fleet(db, station_factory):
    """Mixed fleet: one qemu + one RPi station on v1, both machines have v2.

    Plus an unprovisioned station (no platform).
    """
    q1, r1 = _release("2026.01.01", QEMU), _release("2026.01.01", RPI)
    q2, r2 = _release("2026.02.01", QEMU), _release("2026.02.01", RPI)
    qemu = station_factory(name="Alpha", current_image_release=q1)
    rpi = station_factory(name="Bravo", current_image_release=r1)
    bare = station_factory(name="Charlie")
    return {"q1": q1, "r1": r1, "q2": q2, "r2": r2, "qemu": qemu, "rpi": rpi, "bare": bare}


def _data(version, target_type=Deployment.TargetType.ALL, **extra):
    data = {
        "version": version,
        "target_type": target_type,
        "strategy": Deployment.Strategy.IMMEDIATE,
        "phase_config": "{}",
    }
    data.update(extra)
    return data


@pytest.mark.django_db
class TestVersionChoices:
    def test_grouped_by_channel_with_machines_label(self):
        _release("2026.02.01", QEMU)
        _release("2026.02.01", RPI)
        _release("2026.03.01", QEMU, channel="dev")
        _release("2026.04.01", RPI, ota=False)  # not OTA-ready → not offered

        choices = DeploymentForm().fields["version"].choices
        groups = {label: opts for label, opts in choices if isinstance(opts, list | tuple)}
        assert set(groups) == {"dev", "release"}
        assert list(groups["release"]) == [
            ("release|2026.02.01", f"2026.02.01 · {QEMU}, {RPI}"),
        ]
        assert list(groups["dev"]) == [("dev|2026.03.01", f"2026.03.01 · {QEMU}")]

    def test_unknown_version_rejected(self, fleet):
        form = DeploymentForm(data=_data("release|9.9.9"))
        assert not form.is_valid()
        assert "version" in form.errors

    def test_malformed_version_rejected(self, fleet):
        form = DeploymentForm(data=_data("garbage"))
        assert not form.is_valid()
        assert "version" in form.errors


@pytest.mark.django_db
class TestCreate:
    def test_mixed_fleet_gets_per_machine_images(self, client, staff_user, fleet):
        client.force_login(staff_user)
        response = client.post(
            reverse("deployments:deployment_create"), data=_data("release|2026.02.01")
        )
        assert response.status_code == 302
        dep = Deployment.objects.get()
        assert (dep.image_tag, dep.image_channel) == ("2026.02.01", "release")
        images = {r.station_id: r.image_release_id for r in dep.results.all()}
        assert images == {fleet["qemu"].pk: fleet["q2"].pk, fleet["rpi"].pk: fleet["r2"].pk}
        assert [s["station_name"] for s in dep.skipped_stations] == ["Charlie"]

        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        assert "Deployment created for 2 station(s), 1 skipped." in msgs

        detail = client.get(response["Location"]).content.decode()
        assert "Skipped stations" in detail
        assert "Charlie" in detail
        assert "Platform unknown" in detail
        assert f"2026.02.01 · {RPI}" in detail
        assert f"2026.02.01 · {QEMU}" in detail

    def test_nothing_servable_is_form_error(self, client, staff_user, fleet):
        client.force_login(staff_user)
        response = client.post(
            reverse("deployments:deployment_create"),
            data=_data(
                "release|2026.02.01",
                Deployment.TargetType.STATION,
                target_station=fleet["bare"].pk,
            ),
        )
        assert response.status_code == 200
        assert not Deployment.objects.exists()
        body = response.content.decode()
        assert "Charlie" in body
        assert "Platform unknown" in body

    def test_group_target_required(self, client, staff_user, fleet):
        client.force_login(staff_user)
        response = client.post(
            reverse("deployments:deployment_create"),
            data=_data("release|2026.02.01", Deployment.TargetType.GROUP),
        )
        assert response.status_code == 200
        assert "target_group" in response.context["form"].errors
        assert not Deployment.objects.exists()


@pytest.mark.django_db
class TestPreview:
    url = staticmethod(lambda: reverse("deployments:deployment_preview"))

    def test_rows_ok_skip_switch_downgrade(self, client, staff_user, fleet, make_rollout_group):
        dev = make_rollout_group("Lab", channel=RolloutGroup.Channel.DEV)
        fleet["rpi"].rollout_group = dev
        fleet["rpi"].save(update_fields=["rollout_group"])
        # qemu station already runs v2 → deploying v1 is a downgrade.
        fleet["qemu"].current_image_release = fleet["q2"]
        fleet["qemu"].save(update_fields=["current_image_release"])

        client.force_login(staff_user)
        response = client.get(self.url(), data=_data("release|2026.01.01"))
        assert response.status_code == 200
        rows = {p.station.name: p for p in response.context["plans"]}
        assert rows["Alpha"].ok and rows["Alpha"].downgrade
        assert rows["Bravo"].ok and rows["Bravo"].channel_switch
        assert not rows["Charlie"].ok

        body = response.content.decode()
        assert "2 will be deployed" in body
        assert "1 skipped" in body
        assert "Downgrade" in body
        assert "Channel switch" in body
        assert "Platform unknown" in body
        assert f"2026.01.01 · {RPI}" in body
        assert not Deployment.objects.exists()
        assert not DeploymentResult.objects.exists()

    def test_nothing_servable_still_previews(self, client, staff_user, fleet):
        client.force_login(staff_user)
        response = client.get(
            self.url(),
            data=_data(
                "release|2026.02.01",
                Deployment.TargetType.STATION,
                target_station=fleet["bare"].pk,
            ),
        )
        assert response.status_code == 200
        body = response.content.decode()
        assert "0 will be deployed" in body
        assert "Charlie" in body

    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"version": "garbage"},
            {"version": "release|nope", "target_type": "all"},
            {"version": "release|2026.02.01", "target_type": "group"},
            {"version": "release|2026.02.01", "target_type": "station", "target_station": "x"},
            {"version": "release|2026.02.01", "target_type": "bogus"},
        ],
    )
    def test_bad_params_render_empty_state(self, client, staff_user, fleet, params):
        client.force_login(staff_user)
        response = client.get(self.url(), data=params)
        assert response.status_code == 200
        assert response.context["plans"] is None
        assert "data-preview-empty" in response.content.decode()
        assert not Deployment.objects.exists()


@pytest.mark.django_db
class TestPermissions:
    @pytest.mark.parametrize(
        "name", ["deployments:deployment_create", "deployments:deployment_preview"]
    )
    def test_member_forbidden(self, client, member_user, name):
        client.force_login(member_user)
        assert client.get(reverse(name)).status_code == 403

    def test_member_cannot_post_create(self, client, member_user, fleet):
        client.force_login(member_user)
        response = client.post(
            reverse("deployments:deployment_create"), data=_data("release|2026.02.01")
        )
        assert response.status_code == 403
        assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_detail_query_count_independent_of_results(
    client, staff_user, station_factory, django_assert_max_num_queries
):
    def make(n):
        q = _release(f"2027.0{n}.01", QEMU)
        dep = Deployment.objects.create(image_tag=q.tag, target_type=Deployment.TargetType.ALL)
        for _i in range(n):
            st = station_factory(current_image_release=q)
            DeploymentResult.objects.create(deployment=dep, station=st, image_release=q)
        return dep

    client.force_login(staff_user)
    small, big = make(1), make(6)
    url = lambda d: reverse("deployments:deployment_detail", kwargs={"pk": d.pk})  # noqa: E731
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        assert client.get(url(small)).status_code == 200
    with django_assert_max_num_queries(len(ctx.captured_queries)):
        response = client.get(url(big))
    assert response.context["machines"] == [QEMU]


def _mid_flight(station, release, status=DeploymentResult.Status.INSTALLING):
    other = Deployment.objects.create(
        image_tag=release.tag,
        image_channel=release.channel,
        target_type=Deployment.TargetType.STATION,
        target_station=station,
        status=Deployment.Status.IN_PROGRESS,
    )
    DeploymentResult.objects.create(
        deployment=other, station=station, image_release=release, status=status
    )
    return other


@pytest.mark.django_db
class TestActiveDeployment:
    def test_preview_flags_superseded_queued_result(self, client, staff_user, fleet):
        queued = _mid_flight(fleet["qemu"], fleet["q1"], status=DeploymentResult.Status.PENDING)
        client.force_login(staff_user)
        response = client.get(
            reverse("deployments:deployment_preview"), data=_data("release|2026.02.01")
        )
        rows = {p.station.name: p for p in response.context["plans"]}
        assert rows["Alpha"].ok and rows["Alpha"].supersedes == queued.pk
        assert rows["Bravo"].supersedes is None
        assert f"Replaces queued #{queued.pk}" in response.content.decode()
        assert DeploymentResult.objects.get(deployment=queued).status == "pending"

    def test_preview_row_shows_active_reason(self, client, staff_user, fleet):
        blocking = _mid_flight(fleet["qemu"], fleet["q1"])
        client.force_login(staff_user)
        response = client.get(
            reverse("deployments:deployment_preview"), data=_data("release|2026.02.01")
        )
        rows = {p.station.name: p for p in response.context["plans"]}
        assert rows["Alpha"].reason == "active_deployment"
        assert rows["Bravo"].ok
        body = response.content.decode()
        assert 'data-plan-reason="active_deployment"' in body
        assert f"deployment #{blocking.pk}" in body
        assert "1 will be deployed" in body

    def test_single_mid_flight_station_is_form_error(self, client, staff_user, fleet):
        blocking = _mid_flight(fleet["qemu"], fleet["q1"])
        form = DeploymentForm(
            data=_data(
                "release|2026.02.01",
                Deployment.TargetType.STATION,
                target_station=fleet["qemu"].pk,
            )
        )
        assert not form.is_valid()  # caught by clean(), not only by the create race guard
        assert any("mid-deployment" in e for e in form.non_field_errors())
        client.force_login(staff_user)
        response = client.post(
            reverse("deployments:deployment_create"),
            data=_data(
                "release|2026.02.01",
                Deployment.TargetType.STATION,
                target_station=fleet["qemu"].pk,
            ),
        )
        assert response.status_code == 200
        assert response.context["form"].non_field_errors()
        assert list(Deployment.objects.values_list("pk", flat=True)) == [blocking.pk]
        assert "mid-deployment" in response.content.decode()

    def test_mixed_fleet_lists_mid_flight_station_as_skipped(self, client, staff_user, fleet):
        blocking = _mid_flight(fleet["qemu"], fleet["q1"])
        client.force_login(staff_user)
        response = client.post(
            reverse("deployments:deployment_create"), data=_data("release|2026.02.01")
        )
        assert response.status_code == 302
        dep = Deployment.objects.exclude(pk=blocking.pk).get()
        assert list(dep.results.values_list("station_id", flat=True)) == [fleet["rpi"].pk]
        reasons = {s["station_name"]: s["reason"] for s in dep.skipped_stations}
        assert reasons == {"Alpha": "active_deployment", "Charlie": "no_platform"}

    def test_create_race_after_clean_renders_form_error(
        self, client, staff_user, fleet, monkeypatch
    ):
        """Servable in clean(), mid-flight by the time create runs → 200, no 500."""
        from apps.deployments import forms

        real_plan = forms.plan_deployment

        def plan_then_go_mid_flight(tag, channel, stations):
            plans = real_plan(tag, channel, stations)
            _mid_flight(fleet["qemu"], fleet["q1"])
            return plans

        monkeypatch.setattr(forms, "plan_deployment", plan_then_go_mid_flight)
        client.force_login(staff_user)
        n_before = Deployment.objects.count()
        response = client.post(
            reverse("deployments:deployment_create"),
            data=_data(
                "release|2026.02.01",
                Deployment.TargetType.STATION,
                target_station=fleet["qemu"].pk,
            ),
        )
        assert response.status_code == 200
        # Only the blocking deployment the patch created — none from the form.
        assert Deployment.objects.count() == n_before + 1
        assert not Deployment.objects.filter(image_tag="2026.02.01").exists()
        errors = response.context["form"].non_field_errors()
        assert errors and "mid-deployment" in errors[0]
        assert "mid-deployment" in response.content.decode()
