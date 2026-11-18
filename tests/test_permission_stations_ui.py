import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _login(client, level):
    u = User.objects.create_user(username=f"s-{level}", password="x", membership_level=level)
    client.force_login(u)
    return u


@pytest.mark.parametrize("level", ["applicant", "member"])
def test_below_staff_sees_station_detail_without_edit_or_audit(client, station, level):
    _login(client, level)
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


@pytest.mark.parametrize("level", ["applicant", "member"])
def test_audit_logs_not_queried_below_staff(client, station, level):
    _login(client, level)
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


@pytest.mark.parametrize("level", ["applicant", "member"])
def test_last_ip_hidden_below_staff_shown_to_staff(client, station, level):
    station.last_ip_address = "203.0.113.77"
    station.save()
    _login(client, level)
    url = reverse("stations:station_detail", args=[station.pk])
    html = client.get(url).content.decode()
    assert "203.0.113.77" not in html
    assert 'data-tab="audit"' not in html
    assert 'data-tab-panel="audit"' not in html
    client.logout()
    _login(client, "staff")
    assert "203.0.113.77" in client.get(url).content.decode()


def test_audit_tab_ip_only_for_admin(client, station):
    from apps.stations.models import StationAuditLog

    StationAuditLog.objects.create(
        station=station,
        event_type=StationAuditLog.EventType.UPDATED,
        message="changed something",
        ip_address="198.51.100.23",
    )
    url = reverse("stations:station_detail", args=[station.pk])
    _login(client, "staff")
    html = client.get(url).content.decode()
    assert 'data-tab-panel="audit"' in html
    assert "changed something" in html
    assert "198.51.100.23" not in html
    assert "<th>IP</th>" not in html
    client.logout()
    _login(client, "admin")
    html = client.get(url).content.decode()
    assert "198.51.100.23" in html
    assert "<th>IP</th>" in html


def test_audit_message_ips_masked_for_non_admin(client, station):
    from apps.stations.models import StationAuditLog

    StationAuditLog.objects.create(
        station=station,
        event_type=StationAuditLog.EventType.HEARTBEAT,
        message="IP address changed: 198.51.100.23 → 2001:db8::1",
    )
    url = reverse("stations:station_detail", args=[station.pk])
    _login(client, "staff")
    html = client.get(url).content.decode()
    assert "IP address changed: [IP] → [IP]" in html
    assert "198.51.100.23" not in html
    assert "2001:db8::1" not in html
    client.logout()
    _login(client, "admin")
    html = client.get(url).content.decode()
    assert "198.51.100.23" in html
    assert "2001:db8::1" in html


def test_mask_ips_filter_keeps_other_text():
    from apps.stations.templatetags.station_audit import mask_ips

    assert mask_ips("OS version changed: 1.2 → 1.3") == "OS version changed: 1.2 → 1.3"
    assert mask_ips("at 10:30:00 from 10.0.0.1") == "at 10:30:00 from [IP]"


def test_mask_ips_filter_embedded_ipv4():
    from apps.stations.templatetags.station_audit import mask_ips

    assert mask_ips("from ::ffff:192.168.1.1 now") == "from [IP] now"
    assert mask_ips("64:ff9b::1.2.3.4") == "[IP]"
