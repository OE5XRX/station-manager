import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog, User

pytestmark = pytest.mark.django_db


def _login(client, level, name=None):
    u = User.objects.create_user(
        username=name or f"x-{level}", password="x", membership_level=level
    )
    client.force_login(u)
    return u


def test_staff_sees_alert_list_and_can_ack(client, alert):
    _login(client, "staff")
    assert client.get(reverse("monitoring:alert_list")).status_code == 200
    assert client.post(reverse("monitoring:alert_acknowledge", args=[alert.pk])).status_code != 403


def test_applicant_sees_alert_settings_read_only(client):
    _login(client, "applicant")
    r = client.get(reverse("monitoring:alert_settings"))
    assert r.status_code == 200 and "nur lesend" in r.content.decode()


def test_staff_cannot_update_alert_rule_or_send_test(client):
    _login(client, "staff")
    assert client.post(reverse("monitoring:test_email")).status_code == 403


def test_staff_gets_module_rights(client):
    _login(client, "staff")
    assert client.get(reverse("module_firmware:gh_partial")).status_code != 403


def test_member_cannot_import_firmware(client):
    _login(client, "member")
    assert client.post(reverse("module_firmware:import"), {}).status_code == 403


def test_applicant_can_view_modules_but_not_manage(client):
    _login(client, "applicant")
    assert client.get(reverse("module_firmware:module_list")).status_code == 200
    assert client.get(reverse("module_firmware:release_list")).status_code == 200


def test_staff_terminal_status_denied(client, station):
    _login(client, "staff")
    assert client.get(reverse("tunnel:status", args=[station.pk])).status_code == 403


def test_admin_terminal_status_ok(client, station):
    _login(client, "admin")
    assert client.get(reverse("tunnel:status", args=[station.pk])).status_code == 200


def test_dashboard_denial_tile_admin_only(client):
    m = _login(client, "member", "den")
    client.get(reverse("audit:audit_list"))  # creates a denial
    assert AccountAuditLog.objects.filter(event_type="permission_denied", actor=m).exists()
    _login(client, "admin", "adm")
    html = client.get(reverse("dashboard:index")).content.decode()
    assert "Zugriffsverweigerungen" in html
    assert "event_type=permission_denied" in html
    r = client.get(reverse("audit:audit_list") + "?event_type=permission_denied")
    assert "audit.view" in r.content.decode()


def test_dashboard_denial_tile_hidden_for_staff(client):
    _login(client, "staff")
    html = client.get(reverse("dashboard:index")).content.decode()
    assert "Zugriffsverweigerungen" not in html


def test_audit_quick_filter_chip_and_details(client):
    m = _login(client, "member", "den2")
    client.get(reverse("audit:audit_list"))
    _login(client, "admin", "adm2")
    html = client.get(reverse("audit:audit_list")).content.decode()
    assert "Verweigerungen" in html
    html = client.get(
        reverse("audit:audit_list") + "?event_type=permission_denied"
    ).content.decode()
    assert m.username in html
    assert "audit.view" in html and "web" in html


def test_dashboard_hides_station_ip_from_non_managers(client, station):
    type(station).objects.filter(pk=station.pk).update(last_ip_address="203.0.113.77")
    _login(client, "member", "ipm")
    assert "203.0.113.77" not in client.get(reverse("dashboard:index")).content.decode()
    _login(client, "staff", "ips")
    assert "203.0.113.77" in client.get(reverse("dashboard:index")).content.decode()


def test_sidebar_has_no_legacy_checks():
    from pathlib import Path

    src = Path("templates/includes/sidebar.html").read_text()
    assert "is_admin" not in src and "is_internal" not in src


def test_alert_recipients_follow_alert_rules_manage(station):
    from apps.monitoring.recipients import email_recipients_for_station_alert

    admin = User.objects.create_user(
        username="a1", password="x", membership_level="admin", email="a1@example.org"
    )
    staff = User.objects.create_user(
        username="s1", password="x", membership_level="staff", email="s1@example.org"
    )
    recipients = list(email_recipients_for_station_alert(station))
    assert admin in recipients and staff not in recipients
