"""UI-Gating folgt den Backend-Checks: kein Link/Button, der auf eine 403 führt.

Alerts = is_admin (AlertListView), Deployments = deployments:view (Applicant+),
Control-Panel = control.view (StationControlView; Applicant+ read-only).
"""

import pytest
from django.urls import reverse

from apps.control.models import StationModule


def _alerts_url():
    return reverse("monitoring:alert_list")


def _deployments_url():
    return reverse("deployments:deployment_list")


@pytest.fixture
def station_with_module(station):
    StationModule.objects.create(
        station=station,
        slot="slot0",
        module_id="fm",
        type="fm",
        capability_descriptor=[],
        online=True,
    )
    return station


def _get(client, user, url):
    client.force_login(user)
    resp = client.get(url)
    assert resp.status_code == 200
    return resp.content.decode()


@pytest.mark.django_db
class TestNavLinksMatchBackend:
    @pytest.mark.parametrize(
        "user_fixture, alerts, deployments",
        [
            ("admin_user", True, True),
            ("operator_user", True, True),
            ("member_user", True, True),
            ("applicant_user", True, True),
        ],
    )
    @pytest.mark.parametrize("page", ["dashboard:index", "stations:station_list"])
    def test_links_only_where_backend_allows(
        self, request, client, user_fixture, alerts, deployments, page
    ):
        user = request.getfixturevalue(user_fixture)
        html = _get(client, user, reverse(page))
        assert (f'href="{_alerts_url()}' in html) is alerts
        assert (f'href="{_deployments_url()}"' in html) is deployments

    @pytest.mark.parametrize(
        "user_fixture, expected_alerts, expected_deployments",
        [
            ("admin_user", 200, 200),
            ("operator_user", 200, 200),
            ("member_user", 200, 200),
            ("applicant_user", 200, 200),
        ],
    )
    def test_backend_contract_unchanged(
        self, request, client, user_fixture, expected_alerts, expected_deployments
    ):
        """Guard: if a view's permission changes, the nav gating above must follow."""
        client.force_login(request.getfixturevalue(user_fixture))
        assert client.get(_alerts_url()).status_code == expected_alerts
        assert client.get(_deployments_url()).status_code == expected_deployments


@pytest.mark.django_db
class TestControlPanelButton:
    @pytest.mark.parametrize(
        "user_fixture, visible",
        [
            ("admin_user", True),
            ("operator_user", True),
            ("member_user", True),
            ("applicant_user", True),  # control:view is Applicant+ (spec 1.3)
        ],
    )
    def test_button_matches_control_view(
        self, request, client, station_with_module, user_fixture, visible
    ):
        user = request.getfixturevalue(user_fixture)
        control_url = reverse("control:station_control", args=[station_with_module.pk])
        html = _get(
            client, user, reverse("stations:station_detail", args=[station_with_module.pk])
        )
        assert (control_url in html) is visible
        page = client.get(control_url)
        assert page.status_code == 200
        body = page.content.decode()
        if user_fixture == "applicant_user":
            # Read-only: no lock/PTT/mic controls, readonly badge, RX audio available.
            assert '@click="acquire()"' not in body
            assert "enableMic()" not in body
            assert 'data-cap="ptt"' not in body
            assert "nur lesend" in body
            assert 'x-data="audioPanel"' in body
        else:
            assert '@click="acquire()"' in body

    def test_applicant_still_sees_station_detail(self, client, applicant_user, station):
        """Applicants dürfen Stationen ansehen — nur nicht steuern."""
        _get(client, applicant_user, reverse("stations:station_detail", args=[station.pk]))


@pytest.mark.django_db
class TestImageReleaseLink:
    @pytest.mark.parametrize(
        "user_fixture, linked",
        [("admin_user", True), ("member_user", True), ("applicant_user", True)],
    )
    def test_images_link_for_everyone_with_images_view(
        self, request, client, station, image_release, user_fixture, linked
    ):
        station.current_image_release = image_release
        station.save()
        html = _get(
            client,
            request.getfixturevalue(user_fixture),
            reverse("stations:station_detail", args=[station.pk]),
        )
        assert (reverse("images:list") in html) is linked
