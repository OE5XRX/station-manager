import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _login(client, level):
    client.force_login(
        User.objects.create_user(username=f"o-{level}", password="x", membership_level=level)
    )


@pytest.mark.parametrize(
    "name", ["deployments:deployment_list", "rollouts:upgrade_dashboard", "images:list"]
)
def test_applicant_can_view_ops_pages(client, name):
    _login(client, "applicant")
    r = client.get(reverse(name))
    assert r.status_code == 200
    assert "nur lesend" in r.content.decode()


def test_staff_can_trigger_station_upgrade_but_not_group(client, station, image_release):
    _login(client, "staff")
    r = client.post(
        reverse("rollouts:upgrade_station", args=[station.pk]),
        {"image_release": image_release.pk},
    )
    assert r.status_code != 403
    assert client.post(reverse("rollouts:upgrade_group", args=["x"]), {}).status_code == 403


def test_staff_cannot_archive_image(client, image_release):
    _login(client, "staff")
    assert client.post(reverse("images:archive", args=[image_release.pk])).status_code == 403


def test_staff_cannot_browse_github_releases(client):
    _login(client, "staff")
    assert client.get(reverse("images:gh_partial")).status_code == 403


def test_staff_cannot_see_provisioning_status(client):
    _login(client, "staff")
    assert (
        client.get(
            reverse("provisioning:status", args=["00000000-0000-0000-0000-000000000000"])
        ).status_code
        == 403
    )


def test_staff_cannot_edit_sequence(client):
    _login(client, "staff")
    assert client.get(reverse("rollouts:sequence_edit")).status_code == 403


def test_applicant_cannot_create_deployment(client):
    _login(client, "applicant")
    assert client.get(reverse("deployments:deployment_create")).status_code == 403


def test_station_detail_cards_follow_permissions(client, station):
    url = reverse("stations:station_detail", args=[station.pk])
    _login(client, "member")
    html = client.get(url).content.decode()
    assert 'data-upgrade-reason="no_platform"' not in html
    assert "<section" in html and ">Provisioning<" not in html
    client.logout()
    _login(client, "staff")
    html = client.get(url).content.decode()
    assert 'data-upgrade-reason="no_platform"' in html  # upgrade card (deployments:manage)
    assert ">Provisioning<" not in html  # provisioning:manage is admin-only
    client.logout()
    _login(client, "admin")
    html = client.get(url).content.decode()
    assert 'data-upgrade-reason="no_platform"' in html
    assert ">Provisioning<" in html


def test_staff_images_page_hides_import_panel(client, image_release):
    _login(client, "staff")
    html = client.get(reverse("images:list")).content.decode()
    assert 'id="import-form"' not in html
    assert "nur lesend" in html


def test_admin_images_page_has_import_panel(client):
    _login(client, "admin")
    html = client.get(reverse("images:list")).content.decode()
    assert 'id="import-form"' in html
