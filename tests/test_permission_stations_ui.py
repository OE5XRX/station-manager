import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _login(client, level):
    u = User.objects.create_user(username=f"s-{level}", password="x", membership_level=level)
    client.force_login(u)
    return u


def test_applicant_sees_station_detail_without_edit_or_audit(client, station):
    _login(client, "applicant")
    r = client.get(reverse("stations:station_detail", args=[station.pk]))
    assert r.status_code == 200
    html = r.content.decode()
    assert reverse("stations:station_edit", args=[station.pk]) not in html
    assert 'data-tab="audit"' not in html
    assert 'data-tab-panel="audit"' not in html
    assert "nur lesend" in html


def test_staff_sees_audit_tab_and_edit(client, station):
    _login(client, "staff")
    html = client.get(reverse("stations:station_detail", args=[station.pk])).content.decode()
    assert reverse("stations:station_edit", args=[station.pk]) in html
    assert 'data-tab="audit"' in html
    assert "nur lesend" not in html


def test_audit_logs_not_queried_for_applicant(client, station, django_assert_max_num_queries):
    _login(client, "applicant")
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        client.get(reverse("stations:station_detail", args=[station.pk]))
    assert not any("stationauditlog" in q["sql"].lower() for q in ctx.captured_queries)


def test_staff_can_edit_station_but_not_set_region(client, station):
    _login(client, "staff")
    assert client.get(reverse("stations:station_edit", args=[station.pk])).status_code == 200
    assert (
        client.post(reverse("stations:station_set_region", args=[station.pk]), {}).status_code
        == 403
    )


def test_staff_detail_has_no_topology_card(client, station):
    _login(client, "staff")
    html = client.get(reverse("stations:station_detail", args=[station.pk])).content.decode()
    assert "station-topology-card" not in html


def test_admin_detail_has_topology_card(client, station):
    _login(client, "admin")
    html = client.get(reverse("stations:station_detail", args=[station.pk])).content.decode()
    assert "station-topology-card" in html


def test_member_cannot_open_create_form(client):
    _login(client, "member")
    assert client.get(reverse("stations:station_create")).status_code == 403


def test_applicant_sees_region_list_read_only(client):
    _login(client, "applicant")
    r = client.get(reverse("stations:region_list"))
    assert r.status_code == 200
    assert reverse("stations:region_create") not in r.content.decode()
    assert "nur lesend" in r.content.decode()


def test_staff_cannot_open_region_create(client):
    _login(client, "staff")
    assert client.get(reverse("stations:region_create")).status_code == 403


def test_staff_cannot_create_assignment(client, station):
    u = _login(client, "staff")
    r = client.post(reverse("accounts:station_assignment_create", args=[u.pk]), {})
    assert r.status_code == 403
    r = client.post(reverse("accounts:region_assignment_create", args=[u.pk]), {})
    assert r.status_code == 403


def test_tag_list_readable_by_member_with_readonly_badge(client):
    _login(client, "member")
    r = client.get(reverse("stations:tag_list"))
    assert r.status_code == 200
    assert "nur lesend" in r.content.decode()


def test_member_cannot_create_tag(client):
    _login(client, "member")
    assert client.get(reverse("stations:tag_create")).status_code == 403


def test_anonymous_redirected_to_login(client):
    assert client.get(reverse("stations:station_list")).status_code == 302


def test_sidebar_lists_tags_and_regions_for_applicant(client):
    _login(client, "applicant")
    html = client.get(reverse("stations:station_list")).content.decode()
    assert reverse("stations:tag_list") in html
    assert reverse("stations:region_list") in html
