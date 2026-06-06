# Membership-Levels + Topology-Roles — PR-2: Notification Routing + Audit Signals + Drop Legacy Groups

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the topology models from PR-1 into the alert-notification pipeline and the audit-log feed, then drop the legacy Django Groups now that membership_level is the single source of truth. After PR-2: alerts route to Station-Admin/Maintainer + Region-Manager + Vereins-Admin, topology mutations emit audit-log entries, /audit/ shows account events.

**Architecture:** New helper module `apps/monitoring/recipients.py` resolves alerts → recipients via topology queries. Audit-log emission moves into `apps/stations/signals.py` with signal handlers registered via `AppConfig.ready()` — covers all save/delete paths (views, Django Admin, ORM). The `/audit/` view merges a third source (`AccountAuditLog`). Final migration drops `admin`/`operator`/`member` groups with a `get_or_create` reverse for 30-day-backup rollback safety.

**Tech Stack:** Django 6.0, pytest-django, django-test-migrations, signal-handler pattern from existing `apps/sso/signals.py` (if present) / standard Django docs.

**Reference spec:** `docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md` §4.6, §4.7, §6.

**Reference PR-1 plan:** `docs/superpowers/plans/2026-06-05-membership-levels-and-topology-roles.md` Phase 4, 5, 9.

**Out of scope (deferred to PR-3):**
- UI: User-Detail rollen-section, Station-Detail topology widgets, Region-CRUD admin page.
- `MEMBERSHIP_PROMOTED` / `MEMBERSHIP_DEMOTED` audit-log emission. The event types live in `AccountAuditLog.EventType` already (from PR-1 Task 6) — the emission happens from the promote/demote view (PR-3 has actor context; a signal-based approach would lose the actor identity).
- Telegram per-user/per-station routing.
- Notification preferences per user.
- Renaming `AdminOrOperatorMixin` / `AdminOrOperatorRequiredMixin` mixins.

---

## In-Tree State After PR-1 Merge (verified 2026-06-06)

| Item | Reality |
|---|---|
| Accounts migrations chain head | `0006_add_account_audit_log` |
| Stations migrations chain head | `0011_add_assignments` |
| `User.membership_level` field | present, TextChoices APPLICANT/MEMBER/STAFF/ADMIN |
| `User.is_admin` | reads `membership_level == ADMIN` |
| `User.is_internal` | reads `membership_level in {STAFF, ADMIN}` |
| `User.is_station_admin(s)` / `is_station_maintainer(s)` / `is_region_manager(r)` | present |
| `User.can_administer_station(s)` / `can_maintain_station(s)` / `can_use_station(s)` | present |
| `Region` model | present in `apps/stations/models.py` (top of file, before StationTag) |
| `Station.region` FK | present, nullable, SET_NULL |
| `StationAssignment` / `RegionAssignment` | present, `_ApplicantForbiddenMixin` enforces clean() |
| `AccountAuditLog` model | present in `apps/accounts/models.py:158`, with EventType + `log()` classmethod |
| `StationAuditLog.EventType` | present at `apps/stations/models.py:311-323`, 13 existing values; signals.py will append 3 more |
| `apps/stations/apps.py` `ready()` | NOT present yet (needs adding for signal registration) |
| `apps/audit/views.py` merge | merges StationAuditLog + SsoAuditLog only (2 sources) |
| `apps/audit/templates/audit/_audit_table.html` | has 2 branches (station / sso) |
| `apps/monitoring/notifications.py` | still hardcoded to `User.objects.filter(membership_level=ADMIN)` (PR-1 Task 10 transitional update) |
| `apps/monitoring/views.py:134-142` `TestNotificationView` | calls `send_test_notification(channel)` — no requesting-user passed |
| Legacy Django Groups | `admin`/`operator`/`member` still exist in `auth_group` table; no call site references them |

---

## File Structure

### New files

| Path | Responsibility |
|---|---|
| `apps/monitoring/recipients.py` | Single-purpose: resolve `alert.station` → email recipient queryset |
| `apps/stations/signals.py` | Signal handlers that emit audit-log entries for topology mutations |
| `apps/accounts/migrations/0007_drop_legacy_role_groups.py` | Data migration: delete `admin`/`operator`/`member` Groups |
| `tests/test_alert_recipients.py` | Unit tests for the recipients helper |
| `tests/test_notification_dispatch.py` | Wiring tests for `_send_email_notification` + test-email path |
| `tests/test_audit_log_emission.py` | Tests that signal handlers emit the right entries on each topology mutation |
| `tests/test_drop_legacy_groups_migration.py` | django-test-migrations test for the group-drop migration |

### Modified files

| Path | Reason |
|---|---|
| `apps/monitoring/notifications.py` | Replace hardcoded admin-level query with `recipients_for_station_alert`; test-email path takes requesting_user |
| `apps/monitoring/views.py` | `TestNotificationView.post` passes `request.user` into `send_test_notification` |
| `apps/stations/models.py` | Extend `StationAuditLog.EventType` with 3 new values |
| `apps/stations/apps.py` | Add `ready()` to register signals |
| `apps/audit/views.py` | Merge AccountAuditLog as 3rd source |
| `apps/audit/templates/audit/_audit_table.html` | New `account` row variant |

---

# Phase 4: Notification Routing

## Task 1: recipients_for_station_alert helper

**Files:**
- Create: `apps/monitoring/recipients.py`
- Create: `tests/test_alert_recipients.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_alert_recipients.py`:

```python
"""Tests for recipients_for_station_alert.

Pins the routing contract: who gets an email for a station alert.
The spec (§4.7) says recipients are:
  - Vereins-Admins (membership_level=ADMIN), vereinsweit
  - Region-Manager of station.region (if set)
  - Station-Admin of this station
  - Station-Maintainer of this station

Excludes Vereins-Staff (operative role, not escalation inbox),
Applicants (defense-in-depth), inactive users, no-email users.
"""

import pytest

from apps.accounts.models import User
from apps.monitoring.recipients import recipients_for_station_alert
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
)


def _user(level, email="x@example.com", username=None):
    username = username or f"u{User.objects.count()}"
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
class TestRecipientsForStationAlert:
    def test_admin_always_recipient(self):
        admin = _user(User.MembershipLevel.ADMIN)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin in list(recipients_for_station_alert(s))

    def test_region_manager_in_set_for_own_region(self):
        mgr = _user(User.MembershipLevel.MEMBER)
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(
            user=mgr, region=r, role=RegionAssignment.Role.MANAGER
        )
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        assert mgr in list(recipients_for_station_alert(s))

    def test_region_manager_not_in_set_for_other_region(self):
        mgr = _user(User.MembershipLevel.MEMBER)
        r1 = Region.objects.create(name="Tirol", slug="tirol")
        r2 = Region.objects.create(name="OOe", slug="ooe")
        RegionAssignment.objects.create(
            user=mgr, region=r1, role=RegionAssignment.Role.MANAGER
        )
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r2)
        assert mgr not in list(recipients_for_station_alert(s))

    def test_station_admin_in_set(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.ADMIN
        )
        assert u in list(recipients_for_station_alert(s))

    def test_station_admin_not_in_set_for_other_station(self):
        u = _user(User.MembershipLevel.MEMBER)
        s1 = Station.objects.create(name="OE5A", callsign="OE5A")
        s2 = Station.objects.create(name="OE5B", callsign="OE5B")
        StationAssignment.objects.create(
            user=u, station=s1, role=StationAssignment.Role.ADMIN
        )
        assert u not in list(recipients_for_station_alert(s2))

    def test_station_maintainer_in_set(self):
        u = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.MAINTAINER
        )
        assert u in list(recipients_for_station_alert(s))

    def test_staff_not_recipient_without_topology(self):
        staff = _user(User.MembershipLevel.STAFF)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert staff not in list(recipients_for_station_alert(s))

    def test_member_without_assignments_not_recipient(self):
        m = _user(User.MembershipLevel.MEMBER)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert m not in list(recipients_for_station_alert(s))

    def test_applicant_never_recipient(self):
        # Applicants cannot hold assignments by model-level invariant
        # (_ApplicantForbiddenMixin). The recipient query additionally
        # excludes them as defense-in-depth.
        a = _user(User.MembershipLevel.APPLICANT)
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert a not in list(recipients_for_station_alert(s))

    def test_dedup_same_user_multiple_roles(self):
        u = _user(User.MembershipLevel.ADMIN)
        r = Region.objects.create(name="Tirol", slug="tirol")
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=r)
        RegionAssignment.objects.create(
            user=u, region=r, role=RegionAssignment.Role.MANAGER
        )
        # Vereins-Admin so the invariant doesn't block the assignment.
        StationAssignment.objects.create(
            user=u, station=s, role=StationAssignment.Role.ADMIN
        )
        recipients = list(recipients_for_station_alert(s))
        assert recipients.count(u) == 1

    def test_inactive_user_excluded(self):
        admin = _user(User.MembershipLevel.ADMIN)
        admin.is_active = False
        admin.save(update_fields=["is_active"])
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin not in list(recipients_for_station_alert(s))

    def test_user_without_email_excluded(self):
        admin = _user(User.MembershipLevel.ADMIN, email="")
        s = Station.objects.create(name="OE5A", callsign="OE5A")
        assert admin not in list(recipients_for_station_alert(s))

    def test_no_region_only_admin_and_station_assignments(self):
        admin = _user(User.MembershipLevel.ADMIN, username="admin")
        # An orphan region-manager exists but the station has no region
        mgr = _user(User.MembershipLevel.MEMBER, username="mgr")
        r = Region.objects.create(name="Tirol", slug="tirol")
        RegionAssignment.objects.create(
            user=mgr, region=r, role=RegionAssignment.Role.MANAGER
        )
        s = Station.objects.create(name="OE5A", callsign="OE5A", region=None)
        rcp = list(recipients_for_station_alert(s))
        assert admin in rcp
        assert mgr not in rcp
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_alert_recipients.py -v
```

Expected: ImportError on `apps.monitoring.recipients`.

- [ ] **Step 3: Create the helper**

Create `apps/monitoring/recipients.py`:

```python
"""Resolve email recipients for a station alert.

Single-responsibility helper. Lives in a dedicated module so the
notification dispatch in apps/monitoring/notifications.py stays
focused on SMTP delivery, and the routing logic is unit-testable in
isolation from email-backend mocking.

Routing contract (see docs/superpowers/specs/
2026-06-05-membership-levels-and-topology-roles-design.md §4.7):
  - Vereins-Admins (membership_level=ADMIN), vereinsweit
  - Region-Manager der zugeh. Region (sofern station.region gesetzt)
  - Station-Admin dieser Station
  - Station-Maintainer dieser Station

Excludes Vereins-Staff (operative role, not escalation inbox),
Applicants (defense-in-depth — invariant blocks them anyway), and
inactive / no-email users.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

User = get_user_model()


def recipients_for_station_alert(station):
    q = Q(membership_level=User.MembershipLevel.ADMIN)

    if station.region_id is not None:
        q |= Q(
            region_assignments__region_id=station.region_id,
            region_assignments__role="manager",
        )

    q |= Q(
        station_assignments__station=station,
        station_assignments__role__in=["admin", "maintainer"],
    )

    return (
        User.objects.filter(q)
        .exclude(email="")
        .exclude(is_active=False)
        .exclude(membership_level=User.MembershipLevel.APPLICANT)
        .distinct()
    )
```

- [ ] **Step 4: Ruff format**

```bash
.venv/bin/ruff format apps/monitoring/recipients.py tests/test_alert_recipients.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 5: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_alert_recipients.py -v
```

Expected: 13 PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/monitoring/recipients.py tests/test_alert_recipients.py
git commit -m "feat(monitoring): recipients_for_station_alert helper

Single-purpose helper resolving alert.station -> queryset of email
recipients via the new topology models. Routes to:
- Vereins-Admins (membership_level=ADMIN)
- Region-Manager of station.region (if set)
- Station-Admin of station
- Station-Maintainer of station
Excludes Vereins-Staff (operative role, not escalation inbox),
Applicants (defense-in-depth), inactive, no-email users."
```

---

## Task 2: Wire helper into _send_email_notification + test-email-to-self

**Files:**
- Modify: `apps/monitoring/notifications.py`
- Modify: `apps/monitoring/views.py` (TestNotificationView)
- Create: `tests/test_notification_dispatch.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_notification_dispatch.py`:

```python
"""Tests for the notification dispatch wiring.

Verifies _send_email_notification routes via recipients_for_station_alert
and that the test-email endpoint sends only to the requesting user.
"""

import logging

import pytest
from django.core import mail
from django.urls import reverse

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station, StationAssignment


def _user(level, email, username):
    u = User.objects.create_user(
        username=username, password="x", email=email
    )
    u.membership_level = level
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
def test_alert_email_goes_to_station_admin_and_vereins_admin(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = (
        "django.core.mail.backends.locmem.EmailBackend"
    )
    mail.outbox = []

    admin = _user(User.MembershipLevel.ADMIN, "admin@x", "admin")
    station_admin = _user(
        User.MembershipLevel.MEMBER, "franz@x", "franz"
    )

    s = Station.objects.create(name="OE5A", callsign="OE5A")
    StationAssignment.objects.create(
        user=station_admin,
        station=s,
        role=StationAssignment.Role.ADMIN,
    )
    rule = AlertRule.objects.get(
        alert_type=AlertRule.AlertType.STATION_OFFLINE
    )
    alert = Alert.objects.create(
        station=s,
        alert_rule=rule,
        severity="critical",
        title="Test",
        message="m",
    )

    send_alert_notifications(alert)

    assert len(mail.outbox) == 1
    sent = mail.outbox[0]
    assert set(sent.to) == {"admin@x", "franz@x"}


@pytest.mark.django_db
def test_test_email_goes_only_to_requesting_admin(client, settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = (
        "django.core.mail.backends.locmem.EmailBackend"
    )
    mail.outbox = []

    admin1 = _user(User.MembershipLevel.ADMIN, "admin1@x", "admin1")
    _user(User.MembershipLevel.ADMIN, "admin2@x", "admin2")

    client.force_login(admin1)
    response = client.post(reverse("monitoring:test_email"))
    assert response.status_code == 200
    assert response.json()["success"] is True

    assert len(mail.outbox) == 1
    assert list(mail.outbox[0].to) == ["admin1@x"]


@pytest.mark.django_db
def test_no_recipients_logs_warning_and_does_not_send(
    settings, caplog
):
    settings.ALERT_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = (
        "django.core.mail.backends.locmem.EmailBackend"
    )
    mail.outbox = []

    # Station without region and no admin user in DB -> empty set
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    rule = AlertRule.objects.get(
        alert_type=AlertRule.AlertType.STATION_OFFLINE
    )
    alert = Alert.objects.create(
        station=s,
        alert_rule=rule,
        severity="critical",
        title="Test",
        message="m",
    )

    with caplog.at_level(
        logging.WARNING, logger="apps.monitoring.notifications"
    ):
        send_alert_notifications(alert)

    assert len(mail.outbox) == 0
    assert any(
        "no recipients" in rec.message.lower() for rec in caplog.records
    )
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_notification_dispatch.py -v
```

Expected:
- `test_alert_email_goes_to_station_admin_and_vereins_admin` FAILs — the current `_send_email_notification` only sends to vereinsweit admins; `franz@x` is missing.
- `test_test_email_goes_only_to_requesting_admin` FAILs — current implementation sends to all admins (`admin1@x` AND `admin2@x`).
- `test_no_recipients_logs_warning_and_does_not_send` may pass or fail depending on warning text.

- [ ] **Step 3: Refactor `_send_email_notification`**

Read `apps/monitoring/notifications.py` and replace the existing `send_alert_notifications` + `_send_email_notification` functions with:

```python
def send_alert_notifications(alert):
    """Dispatch alert via configured channels."""
    if getattr(settings, "ALERT_EMAIL_ENABLED", False):
        _send_email_notification(alert)
    if getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        _send_telegram_notification(alert)


def _send_email_notification(alert, recipients_qs=None):
    """Send the alert email via the topology-based recipient set.

    `recipients_qs` is optional, defaults to recipients_for_station_alert
    for the alert's station. The override exists for the test-email
    path (which scopes to a single user) and for future per-channel
    overrides.
    """
    if recipients_qs is None:
        from apps.monitoring.recipients import (
            recipients_for_station_alert,
        )

        recipients_qs = recipients_for_station_alert(alert.station)

    recipient_list = list(
        recipients_qs.values_list("email", flat=True)
    )
    if not recipient_list:
        region = (
            alert.station.region.name
            if alert.station.region
            else None
        )
        logger.warning(
            "Alert %s on station %s (region=%s) has no recipients. "
            "Configure Station-Admin, Region-Manager, or ensure a "
            "Vereins-Admin has an email set.",
            alert.pk,
            alert.station.name,
            region,
        )
        return

    subject = (
        f"[OE5XRX] {alert.get_severity_display()}: {alert.title}"
    )
    body = (
        f"Station: {alert.station.name}\n"
        f"Severity: {alert.get_severity_display()}\n"
        f"Alert: {alert.title}\n\n"
        f"{alert.message}\n\n"
        f"Time: {alert.created_at}\n"
    )
    try:
        send_mail(
            subject=subject,
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        logger.info(
            "Alert email sent to %d recipient(s).", len(recipient_list)
        )
    except Exception:
        logger.exception("Failed to send alert email.")
```

Drop the now-unused `from apps.accounts.models import User` at the top of the file if no other function uses it. Check by reading the remaining code.

Now update `_test_email` to accept a requesting user:

```python
def _test_email(requesting_user=None):
    """Send a test email to verify SMTP wiring.

    If `requesting_user` is given (the admin who clicked the button),
    the mail goes only to that user's email. This avoids cross-
    notification noise when several admins are configured.
    """
    if not getattr(settings, "ALERT_EMAIL_ENABLED", False):
        return False, (
            "Email notifications are not enabled "
            "(ALERT_EMAIL_ENABLED)."
        )

    if requesting_user is not None and requesting_user.email:
        recipient_list = [requesting_user.email]
    else:
        from apps.accounts.models import User as UserModel

        recipient_list = list(
            UserModel.objects.filter(
                membership_level=UserModel.MembershipLevel.ADMIN
            )
            .exclude(email="")
            .values_list("email", flat=True)
        )

    if not recipient_list:
        return False, (
            "No recipient — set your user's email or configure a "
            "Vereins-Admin with email."
        )

    try:
        send_mail(
            subject="[OE5XRX] Test notification",
            message=(
                f"This is a test notification from OE5XRX "
                f"Station Manager.\n"
                f"Sent at: {timezone.now()}\n\n"
                f"If you received this, email notifications are "
                f"working correctly."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipient_list,
            fail_silently=False,
        )
        return True, ""
    except Exception as e:
        return False, str(e)
```

Update `send_test_notification`:

```python
def send_test_notification(channel, requesting_user=None):
    if channel == "email":
        return _test_email(requesting_user=requesting_user)
    elif channel == "telegram":
        return _test_telegram()
    return False, f"Unknown channel: {channel}"
```

- [ ] **Step 4: Wire requesting_user in the view**

In `apps/monitoring/views.py`, replace `TestNotificationView` (around lines 134-142) with:

```python
class TestNotificationView(AdminRequiredMixin, View):
    def post(self, request, channel):
        success, error_message = send_test_notification(
            channel, requesting_user=request.user
        )
        return JsonResponse(
            {
                "success": success,
                "error": error_message,
            }
        )
```

- [ ] **Step 5: Ruff format**

```bash
.venv/bin/ruff format apps/monitoring/notifications.py apps/monitoring/views.py tests/test_notification_dispatch.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 6: Run tests to verify pass**

```bash
.venv/bin/python -m pytest tests/test_notification_dispatch.py tests/test_alert_recipients.py -v
```

Expected: 16 PASS (3 dispatch + 13 recipient).

- [ ] **Step 7: Full-suite regression**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/monitoring/notifications.py apps/monitoring/views.py tests/test_notification_dispatch.py
git commit -m "feat(monitoring): wire recipients helper + scope test-email to requester

_send_email_notification now resolves recipients via the topology
helper (Station-Admin + Maintainer + Region-Manager + Vereins-Admin).
send_test_notification(email) targets only the requesting admin —
resolves the multi-admin cross-notification annoyance.

The test-email path keeps a fallback to ADMIN-level users (in case
the requesting_user has no email set), so the operator-facing UX
still produces actionable feedback."
```

---

# Phase 5: Audit Log Signal Emission

## Task 3: Extend StationAuditLog.EventType with 3 new values

**Files:**
- Modify: `apps/stations/models.py` (extend `StationAuditLog.EventType`)
- Modify: `tests/test_audit_log_emission.py` (created later; we add the test here as a forward reference)

Note: `choices=` on a CharField is enforced at form/clean level, NOT at the DB level. Adding choices does NOT require a migration. The implementer should verify this with `python manage.py makemigrations --check --dry-run stations` after the change.

- [ ] **Step 1: Add the new event-type values**

In `apps/stations/models.py`, find the `StationAuditLog.EventType` class (around line 311) and append three new choices. Place them with the existing ones to maintain alphabetical-by-domain grouping:

Find this block:
```python
    class EventType(models.TextChoices):
        CREATED = "created", _("Created")
        UPDATED = "updated", _("Updated")
        DELETED = "deleted", _("Deleted")
        STATUS_CHANGE = "status_change", _("Status Change")
        HEARTBEAT = "heartbeat", _("Heartbeat")
        TOKEN_GENERATED = "token_generated", _("Token Generated")
        TOKEN_REVOKED = "token_revoked", _("Token Revoked")
        FIRMWARE_UPDATE = "firmware_update", _("Firmware Update")
        PROVISIONING_REQUESTED = "provisioning_requested", _("Provisioning Requested")
        PROVISIONING_READY = "provisioning_ready", _("Provisioning Ready")
        PROVISIONING_DOWNLOADED = "provisioning_downloaded", _("Provisioning Downloaded")
        PROVISIONING_FAILED = "provisioning_failed", _("Provisioning Failed")
        PROVISIONING_EXPIRED = "provisioning_expired", _("Provisioning Expired")
```

Append (keep one blank line separation if file style uses it):

```python
        STATION_ASSIGNMENT_CREATED = "station_assignment_created", _("Station Assignment Created")
        STATION_ASSIGNMENT_REVOKED = "station_assignment_revoked", _("Station Assignment Revoked")
        STATION_REGION_CHANGED = "station_region_changed", _("Station Region Changed")
```

- [ ] **Step 2: Verify no migration is needed**

```bash
.venv/bin/python manage.py makemigrations --check --dry-run stations
```

Expected: `No changes detected in app 'stations'`.

If Django suggests a migration: read the migration's plan — if it only adds a `choices` field, it's an unnecessary migration (Django sometimes auto-generates these). Accept it if generated; the codebase prefers explicit migrations over silently-different state.

- [ ] **Step 3: Ruff format**

```bash
.venv/bin/ruff format apps/stations/models.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 4: Run the existing tests as a regression-guard**

```bash
.venv/bin/python -m pytest tests/test_topology_models.py tests/test_account_audit_log.py -v 2>&1 | tail -5
```

Expected: all PASS (existing tests unchanged; we're just extending an enum).

- [ ] **Step 5: Commit**

```bash
git add apps/stations/models.py
git commit -m "feat(stations): extend StationAuditLog.EventType with topology events

Adds STATION_ASSIGNMENT_CREATED, STATION_ASSIGNMENT_REVOKED, and
STATION_REGION_CHANGED. choices= changes are not DB-enforced, so
no migration is required (verified via makemigrations --check)."
```

---

## Task 4: Signal handlers for topology audit-log emission

**Files:**
- Create: `apps/stations/signals.py`
- Modify: `apps/stations/apps.py` (add `ready()`)
- Create: `tests/test_audit_log_emission.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_audit_log_emission.py`:

```python
"""Tests that each topology mutation emits the right audit-log entry."""

import pytest

from apps.accounts.models import AccountAuditLog, User
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationAuditLog,
)


def _admin():
    u = User.objects.create_user(
        username="admin", password="x", email="a@x"
    )
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    return u


def _member(name):
    u = User.objects.create_user(
        username=name, password="x", email=f"{name}@x"
    )
    u.membership_level = User.MembershipLevel.MEMBER
    u.save(update_fields=["membership_level"])
    return u


@pytest.mark.django_db
def test_station_assignment_create_emits_audit_log():
    admin = _admin()
    franz = _member("franz")
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    StationAssignment.objects.create(
        user=franz,
        station=s,
        role=StationAssignment.Role.ADMIN,
        assigned_by=admin,
    )
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        station=s,
    ).first()
    assert entry is not None
    assert entry.user == admin  # actor is the assigned_by
    assert "franz" in entry.message.lower()
    assert "admin" in entry.message.lower()


@pytest.mark.django_db
def test_station_assignment_revoke_emits_audit_log():
    admin = _admin()
    franz = _member("franz")
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    a = StationAssignment.objects.create(
        user=franz,
        station=s,
        role=StationAssignment.Role.MAINTAINER,
        assigned_by=admin,
    )
    a.delete()
    entry = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        station=s,
    ).first()
    assert entry is not None
    assert "franz" in entry.message.lower()


@pytest.mark.django_db
def test_station_region_change_emits_audit_log():
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    r1 = Region.objects.create(name="Tirol", slug="tirol")
    r2 = Region.objects.create(name="OOe", slug="ooe")
    s.region = r1
    s.save()
    s.region = r2
    s.save()
    # We expect at least one CHANGED event with a meaningful message.
    entries = list(
        StationAuditLog.objects.filter(
            event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
            station=s,
        ).order_by("created_at")
    )
    assert len(entries) >= 1
    last = entries[-1]
    assert "tirol" in last.message.lower() or "ooe" in last.message.lower()


@pytest.mark.django_db
def test_station_region_unchanged_does_not_emit():
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    r = Region.objects.create(name="Tirol", slug="tirol")
    s.region = r
    s.save()
    # Save again without changing region
    s.callsign = "OE5XYZ"
    s.save()
    entries = StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
        station=s,
    )
    # Exactly one entry (the first set), not two.
    assert entries.count() == 1


@pytest.mark.django_db
def test_region_assignment_create_emits_audit_log():
    admin = _admin()
    lisa = _member("lisa")
    r = Region.objects.create(name="Tirol", slug="tirol")
    RegionAssignment.objects.create(
        user=lisa,
        region=r,
        role=RegionAssignment.Role.MANAGER,
        assigned_by=admin,
    )
    entry = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
        target_user=lisa,
        region=r,
    ).first()
    assert entry is not None
    assert entry.actor == admin


@pytest.mark.django_db
def test_region_assignment_revoke_emits_audit_log():
    admin = _admin()
    lisa = _member("lisa")
    r = Region.objects.create(name="Tirol", slug="tirol")
    a = RegionAssignment.objects.create(
        user=lisa,
        region=r,
        role=RegionAssignment.Role.MANAGER,
        assigned_by=admin,
    )
    a.delete()
    entry = AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        target_user=lisa,
    ).first()
    assert entry is not None


@pytest.mark.django_db
def test_region_create_update_delete_emits_audit_log():
    r = Region.objects.create(name="Innviertel", slug="innv")
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_CREATED,
        region=r,
    ).exists()

    r.name = "Innviertel-West"
    r.save()
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_UPDATED,
        region=r,
    ).exists()

    r.delete()
    # After delete, region FK becomes NULL; query by event_type only.
    assert AccountAuditLog.objects.filter(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
    ).exists()
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py -v
```

Expected: 7 failures (no signal handlers wired yet — entries don't get created).

- [ ] **Step 3: Create the signals module**

Create `apps/stations/signals.py`:

```python
"""Signal handlers that emit audit-log entries for topology mutations.

Signal-based emission catches every save/delete path: views, Django
Admin, shell, direct ORM. Migration 0005 (Group → membership_level
seed) does NOT emit because data migrations run before AppConfig.ready
registers these handlers — that's the documented limitation in the
spec.

Membership-level promote/demote audit emission is NOT here: it lives
in the promote/demote view (PR-3) because the view has the actor
context that signals lack.

Region.region change uses a pre_save + post_save pair: pre_save
records the pre-mutation FK so post_save can compute the diff. We
stash the diff on the instance via a private attribute that gets
deleted after emission.
"""

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.accounts.models import AccountAuditLog
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationAuditLog,
)


# --- StationAssignment ---


@receiver(post_save, sender=StationAssignment)
def _on_station_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    StationAuditLog.objects.create(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        station=instance.station,
        user=instance.assigned_by,
        message=f"{instance.user} → {instance.get_role_display()}",
    )


@receiver(post_delete, sender=StationAssignment)
def _on_station_assignment_delete(sender, instance, **kwargs):
    StationAuditLog.objects.create(
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        station=instance.station,
        user=None,
        message=(
            f"{instance.user} ({instance.get_role_display()}) entfernt"
        ),
    )


# --- Station.region ---

_PENDING_REGION_ATTR = "_pending_region_change"


@receiver(pre_save, sender=Station)
def _on_station_pre_save(sender, instance, **kwargs):
    if not instance.pk:
        return
    try:
        old = Station.objects.only("region_id").get(pk=instance.pk)
    except Station.DoesNotExist:
        return
    if old.region_id != instance.region_id:
        setattr(
            instance,
            _PENDING_REGION_ATTR,
            (old.region_id, instance.region_id),
        )


@receiver(post_save, sender=Station)
def _on_station_save(sender, instance, created, **kwargs):
    change = getattr(instance, _PENDING_REGION_ATTR, None)
    if not change:
        return
    old_id, new_id = change
    old_name = (
        Region.objects.filter(pk=old_id)
        .values_list("name", flat=True)
        .first()
        if old_id
        else None
    )
    new_name = (
        Region.objects.filter(pk=new_id)
        .values_list("name", flat=True)
        .first()
        if new_id
        else None
    )
    StationAuditLog.objects.create(
        event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
        station=instance,
        user=None,
        message=f"{old_name or '∅'} → {new_name or '∅'}",
    )
    delattr(instance, _PENDING_REGION_ATTR)


# --- RegionAssignment ---


@receiver(post_save, sender=RegionAssignment)
def _on_region_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
        actor=instance.assigned_by,
        target_user=instance.user,
        region=instance.region,
        message=f"role={instance.get_role_display()}",
    )


@receiver(post_delete, sender=RegionAssignment)
def _on_region_assignment_delete(sender, instance, **kwargs):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        target_user=instance.user,
        region=instance.region,
        message=f"role={instance.get_role_display()} entfernt",
    )


# --- Region ---


@receiver(post_save, sender=Region)
def _on_region_save(sender, instance, created, **kwargs):
    if created:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_CREATED,
            region=instance,
            message=f"created: {instance.name}",
        )
    else:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_UPDATED,
            region=instance,
            message=f"updated: {instance.name}",
        )


@receiver(post_delete, sender=Region)
def _on_region_delete(sender, instance, **kwargs):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
        region=None,  # FK is gone after delete
        message=f"deleted: {instance.name}",
    )
```

- [ ] **Step 4: Register signals via AppConfig.ready**

Replace the contents of `apps/stations/apps.py`:

```python
from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _


class StationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.stations"
    verbose_name = _("Stations")

    def ready(self):
        # Register signal handlers for topology audit-log emission.
        # Import inside ready() per Django convention to avoid
        # AppRegistry-not-ready issues at startup.
        from apps.stations import signals  # noqa: F401
```

- [ ] **Step 5: Ruff format**

```bash
.venv/bin/ruff format apps/stations/signals.py apps/stations/apps.py tests/test_audit_log_emission.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 6: Run the audit-emission tests**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py -v
```

Expected: 7 PASS.

- [ ] **Step 7: Full-suite regression**

The new signals fire on EVERY StationAssignment/RegionAssignment/Region/Station save. Run the full suite to catch any test that creates these without expecting an audit-log entry to appear:

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -10
```

Expected: all PASS. If a test fails because it asserts an unexpected absence (e.g., `Region.objects.count()` is expected to be 1 but is now 0 because… no, signals don't change object counts; they ADD audit-log entries). Most likely no failures.

If any test fails because the new signals emit StationAuditLog entries that the existing audit-feed test didn't expect: that test would need to be updated to filter out the new event types or accept the additional entries. Treat as collateral damage, fix minimally.

- [ ] **Step 8: Commit**

```bash
git add apps/stations/signals.py apps/stations/apps.py tests/test_audit_log_emission.py
git commit -m "feat(stations): audit-log signals for topology mutations

7 signal handlers emit audit-log entries on every save/delete path
(views, Django Admin, shell, ORM):
- StationAssignment create/delete -> StationAuditLog
- Station.region change          -> StationAuditLog
- RegionAssignment create/delete -> AccountAuditLog
- Region create/update/delete    -> AccountAuditLog

Membership-level promote/demote emission is deferred to PR-3
(its dedicated view has the actor context that signals lack).
Migration 0005's group-seeded data does not emit (data migrations
run before AppConfig.ready registers handlers — documented in
the spec)."
```

---

## Task 5: Merge AccountAuditLog into apps/audit/ feed

**Files:**
- Modify: `apps/audit/views.py`
- Modify: `apps/audit/templates/audit/_audit_table.html`
- Append tests to: `tests/test_audit_log_emission.py`

- [ ] **Step 1: Append failing test**

Append to `tests/test_audit_log_emission.py`:

```python
@pytest.mark.django_db
def test_audit_events_visible_in_merged_feed(client):
    admin = _admin()
    # Generate one event in each of the 3 sources
    Region.objects.create(name="Tirol", slug="tirol")  # AccountAuditLog REGION_CREATED
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    # Force a station audit entry — the easiest: emit via the log helper
    StationAuditLog.log(
        station=s,
        event_type=StationAuditLog.EventType.UPDATED,
        message="manual log for merged-feed test",
        user=admin,
    )

    client.force_login(admin)
    response = client.get("/audit/")
    assert response.status_code == 200
    body = response.content.decode()
    # REGION_CREATED entry should surface (AccountAuditLog source)
    assert "Tirol" in body
    # UPDATED entry should surface (StationAuditLog source)
    assert "manual log for merged-feed test" in body
```

- [ ] **Step 2: Run to verify the merged-feed assertion fails for AccountAuditLog**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py::test_audit_events_visible_in_merged_feed -v
```

Expected: the StationAuditLog entry shows up (existing merge), but `"Tirol"` from the AccountAuditLog entry is missing → FAIL.

- [ ] **Step 3: Update `apps/audit/views.py`**

Add the AccountAuditLog import at the top of the file (alongside the existing model imports):

```python
from apps.accounts.models import AccountAuditLog
```

In `AuditLogListView.get_queryset()`, extend the merge logic to include AccountAuditLog. Read the current method body carefully — the merge mode is at the end of the method. Replace the section starting with `# Merge mode.` (around line 124) and the existing merge-mode block with:

```python
        # Merge mode.
        self._single_source = None
        station_qs = StationAuditLog.objects.select_related(
            "station", "user"
        )
        station_qs = self.apply_filters(station_qs, params)
        station_entries = list(
            station_qs.order_by("-created_at")[:MERGE_FEED_CAP]
        )

        sso_qs = SsoAuditLog.objects.select_related(
            "actor", "target_user", "application"
        )
        sso_qs = self.apply_sso_date_filters(sso_qs, params)
        sso_entries = list(
            sso_qs.order_by("-created_at")[:MERGE_FEED_CAP]
        )

        account_qs = AccountAuditLog.objects.select_related(
            "actor", "target_user", "region"
        )
        account_qs = self.apply_sso_date_filters(account_qs, params)
        account_entries = list(
            account_qs.order_by("-created_at")[:MERGE_FEED_CAP]
        )

        merged = (
            [("station", e) for e in station_entries]
            + [("sso", e) for e in sso_entries]
            + [("account", e) for e in account_entries]
        )
        merged.sort(
            key=lambda pair: pair[1].created_at, reverse=True
        )
        return merged
```

The narrowing logic at the top of the method (`station_only_filters_active`, `include_station`, `include_sso`) needs an `include_account`. Replace the existing block:

```python
        station_only_filters_active = any(
            [
                params.get("station"),
                params.get("event_type"),
                params.get("user"),
            ]
        )
        include_station = category in ("", "station")
        include_sso = category in ("", "sso") and not (
            category == "" and station_only_filters_active
        )
```

with:

```python
        station_only_filters_active = any(
            [
                params.get("station"),
                params.get("event_type"),
                params.get("user"),
            ]
        )
        include_station = category in ("", "station")
        include_sso = category in ("", "sso") and not (
            category == "" and station_only_filters_active
        )
        include_account = category in ("", "account") and not (
            category == "" and station_only_filters_active
        )
```

Also add single-source mode for the account category. Find the existing two single-source blocks:

```python
        if not merging and include_station:
            station_qs = StationAuditLog.objects.select_related("station", "user")
            station_qs = self.apply_filters(station_qs, params)
            self._single_source = "station"
            return station_qs.order_by("-created_at")

        if not merging and include_sso:
            sso_qs = SsoAuditLog.objects.select_related("actor", "target_user", "application")
            sso_qs = self.apply_sso_date_filters(sso_qs, params)
            self._single_source = "sso"
            return sso_qs.order_by("-created_at")
```

Update the `merging` calculation to include `include_account`, and add an account single-source block:

```python
        # Single-source mode (only one feed active) returns the raw
        # queryset so Paginator can LIMIT/OFFSET at the DB level.
        # Merge mode (2+ sources) materializes a sorted list of
        # (category, entry) tuples, capped at MERGE_FEED_CAP per source.
        active_count = sum(
            [include_station, include_sso, include_account]
        )
        merging = active_count > 1

        if not merging and include_station:
            station_qs = StationAuditLog.objects.select_related(
                "station", "user"
            )
            station_qs = self.apply_filters(station_qs, params)
            self._single_source = "station"
            return station_qs.order_by("-created_at")

        if not merging and include_sso:
            sso_qs = SsoAuditLog.objects.select_related(
                "actor", "target_user", "application"
            )
            sso_qs = self.apply_sso_date_filters(sso_qs, params)
            self._single_source = "sso"
            return sso_qs.order_by("-created_at")

        if not merging and include_account:
            account_qs = AccountAuditLog.objects.select_related(
                "actor", "target_user", "region"
            )
            account_qs = self.apply_sso_date_filters(
                account_qs, params
            )
            self._single_source = "account"
            return account_qs.order_by("-created_at")
```

- [ ] **Step 4: Update the audit template**

Read `apps/audit/templates/audit/_audit_table.html`. It has two `{% if row_category == "station" %}` / `{% else %}` branches. Add a third branch for account:

Find the existing block:

```html
        {% if row_category == "station" %}
          ... (station-row rendering) ...
        {% else %}
          ... (sso-row rendering) ...
        {% endif %}
```

Restructure to:

```html
        {% if row_category == "station" %}
          <td data-label="{% trans 'Category' %}"><span class="pill pill-muted">{% trans "Station" %}</span></td>
          <td data-label="{% trans 'Subject' %}">
            {% if entry.station %}
              <a href="{% url 'stations:station_detail' entry.station.pk %}" class="callsign">{{ entry.station.callsign|default:entry.station.name }}</a>
            {% else %}<span class="t-muted">—</span>{% endif %}
          </td>
          <td data-label="{% trans 'Event' %}"><span class="pill pill-muted">{{ entry.get_event_type_display|upper }}</span></td>
          <td data-label="{% trans 'Message' %}">{{ entry.message }}</td>
          <td class="t-mono-sm" data-label="{% trans 'User' %}">{{ entry.user.username|default:"—" }}</td>
          <td class="t-mono-sm" data-label="{% trans 'IP' %}">{{ entry.ip_address|default:"—" }}</td>
        {% elif row_category == "account" %}
          <td data-label="{% trans 'Category' %}"><span class="pill pill-info">{% trans "Account" %}</span></td>
          <td data-label="{% trans 'Subject' %}">
            {% if entry.target_user %}{{ entry.target_user.username }}
            {% elif entry.region %}{{ entry.region.name }}
            {% else %}<span class="t-muted">—</span>{% endif %}
          </td>
          <td data-label="{% trans 'Event' %}"><span class="pill pill-muted">{{ entry.get_event_type_display|upper }}</span></td>
          <td data-label="{% trans 'Message' %}">{{ entry.message }}</td>
          <td class="t-mono-sm" data-label="{% trans 'User' %}">{{ entry.actor.username|default:"—" }}</td>
          <td class="t-mono-sm" data-label="{% trans 'IP' %}">{{ entry.ip_address|default:"—" }}</td>
        {% else %}
          <td data-label="{% trans 'Category' %}"><span class="pill pill-info">SSO</span></td>
          <td data-label="{% trans 'Subject' %}">
            {% if entry.application %}{{ entry.application.name }}
            {% elif entry.target_user %}{{ entry.target_user.username }}
            {% else %}<span class="t-muted">—</span>{% endif %}
          </td>
          <td data-label="{% trans 'Event' %}"><span class="pill pill-muted">{{ entry.get_event_type_display|upper }}</span></td>
          <td data-label="{% trans 'Message' %}">{{ entry.message }}</td>
          <td class="t-mono-sm" data-label="{% trans 'User' %}">{{ entry.actor.username|default:"—" }}</td>
          <td class="t-mono-sm" data-label="{% trans 'IP' %}">{{ entry.ip_address|default:"—" }}</td>
        {% endif %}
```

- [ ] **Step 5: Ruff format**

```bash
.venv/bin/ruff format apps/audit/views.py tests/test_audit_log_emission.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 6: Run audit tests**

```bash
.venv/bin/python -m pytest tests/test_audit_log_emission.py -v
```

Expected: 8 PASS (7 emission + 1 merged-feed).

- [ ] **Step 7: Full-suite regression**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/audit/views.py apps/audit/templates/audit/_audit_table.html tests/test_audit_log_emission.py
git commit -m "feat(audit): merge AccountAuditLog into /audit/ feed (3 sources)

The merged audit feed now shows StationAuditLog (per-station events) +
SsoAuditLog (SSO/OIDC) + AccountAuditLog (membership + topology).
Single-source mode also supports category=account; the merge-mode
narrowing logic treats account the same as sso (excluded when
station-only filters are active)."
```

---

# Phase 9: Drop Legacy Django Groups

## Task 6: Migration 0007 — delete admin/operator/member groups

**Files:**
- Create: `apps/accounts/migrations/0007_drop_legacy_role_groups.py`
- Create: `tests/test_drop_legacy_groups_migration.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_drop_legacy_groups_migration.py`:

```python
"""Verify migration 0007 deletes the legacy role-groups and that the
reverse-code re-creates them for rollback safety."""

import pytest


@pytest.mark.django_db(transaction=True)
def test_0007_drops_legacy_groups(migrator):
    old_state = migrator.apply_initial_migration(
        [("accounts", "0006_add_account_audit_log")]
    )
    Group = old_state.apps.get_model("auth", "Group")
    for name in ("admin", "operator", "member"):
        Group.objects.get_or_create(name=name)
    assert (
        Group.objects.filter(
            name__in=["admin", "operator", "member"]
        ).count()
        == 3
    )

    new_state = migrator.apply_tested_migration(
        [("accounts", "0007_drop_legacy_role_groups")]
    )
    Group = new_state.apps.get_model("auth", "Group")
    assert (
        Group.objects.filter(
            name__in=["admin", "operator", "member"]
        ).count()
        == 0
    )


@pytest.mark.django_db(transaction=True)
def test_0007_reverse_recreates_groups(migrator):
    old_state = migrator.apply_initial_migration(
        [("accounts", "0006_add_account_audit_log")]
    )
    Group = old_state.apps.get_model("auth", "Group")
    for name in ("admin", "operator", "member"):
        Group.objects.get_or_create(name=name)

    new_state = migrator.apply_tested_migration(
        [("accounts", "0007_drop_legacy_role_groups")]
    )
    Group = new_state.apps.get_model("auth", "Group")
    assert (
        Group.objects.filter(
            name__in=["admin", "operator", "member"]
        ).count()
        == 0
    )

    # Reverse to 0006
    final_state = migrator.apply_tested_migration(
        [("accounts", "0006_add_account_audit_log")]
    )
    Group = final_state.apps.get_model("auth", "Group")
    assert (
        Group.objects.filter(
            name__in=["admin", "operator", "member"]
        ).count()
        == 3
    )
```

- [ ] **Step 2: Run to verify failure**

```bash
.venv/bin/python -m pytest tests/test_drop_legacy_groups_migration.py -v
```

Expected: both tests ERROR with `Migration accounts.0007_drop_legacy_role_groups does not exist`.

- [ ] **Step 3: Create the migration**

Create `apps/accounts/migrations/0007_drop_legacy_role_groups.py`:

```python
"""Delete the legacy Django role-groups (admin/operator/member).

After PR-1 (User.is_admin reads membership_level + 13-call-site
refactor + conftest fixtures stopped dual-writing groups), the groups
themselves serve no further purpose. They are removed here.

Reverse re-creates the groups via idempotent get_or_create so that a
rollback within the 30-day backup window leaves the database
recoverable. The reversed groups are EMPTY — membership reconstruction
is not attempted (cardinal: rollback inside the backup window relies
on the pre-cutover backup itself, not on reverse-migration round-trip
fidelity).
"""

from django.db import migrations

LEGACY_GROUPS = ("admin", "operator", "member")


def drop_legacy_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=LEGACY_GROUPS).delete()


def recreate_legacy_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in LEGACY_GROUPS:
        Group.objects.get_or_create(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0006_add_account_audit_log"),
    ]

    operations = [
        migrations.RunPython(
            drop_legacy_groups,
            reverse_code=recreate_legacy_groups,
        ),
    ]
```

- [ ] **Step 4: Ruff format**

```bash
.venv/bin/ruff format apps/accounts/migrations/0007_drop_legacy_role_groups.py tests/test_drop_legacy_groups_migration.py
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

- [ ] **Step 5: Run tests**

```bash
.venv/bin/python -m pytest tests/test_drop_legacy_groups_migration.py -v
```

Expected: 2 PASS.

- [ ] **Step 6: Full-suite regression**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -5
```

Expected: all PASS. (No test should depend on the legacy groups existing — the conftest dual-write was stripped in PR-1 Task 10.)

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/migrations/0007_drop_legacy_role_groups.py tests/test_drop_legacy_groups_migration.py
git commit -m "feat(accounts): drop legacy admin/operator/member Django Groups

Cutover complete: membership_level is now the single source of truth
for the vereinsweit role state. Legacy groups removed. Reverse-code
recreates empty groups via get_or_create for 30-day backup-window
rollback safety; real rollback after that window relies on the
pre-cutover backup, not on migration-reverse round-trip fidelity."
```

---

# Wrap-Up

## Task 7: Full-suite regression run

**Files:** (none modified)

- [ ] **Step 1: Run full suite + lint**

```bash
.venv/bin/python -m pytest -q 2>&1 | tail -10
.venv/bin/ruff format --check . && .venv/bin/ruff check .
```

Expected: all PASS + ruff clean.

If lint changes anything, run `.venv/bin/ruff format .` and amend the relevant last commit (`git commit --amend --no-edit`).

---

## Task 8: Push branch + create PR

**Files:** (none modified)

- [ ] **Step 1: Push**

```bash
git push -u origin feat/membership-levels-pr2-routing-audit-drop
```

(Or whatever the actual branch name is — branch is created by the controller before dispatching Task 1.)

- [ ] **Step 2: Open PR**

```bash
gh pr create --title "feat(monitoring+stations+accounts): notification routing + audit signals + drop legacy groups (PR-2)" --body "$(cat <<'EOF'
## Summary

**PR-2 of 3** — Routing + Audit + Cleanup. Wires the topology models
from PR-1 into the alert pipeline and the audit-log feed, then drops
the legacy Django Groups now that membership_level is the single
source of truth. UI for topology management is PR-3.

## What this PR does

**Phase 4 — Notification routing:**
- New \`apps/monitoring/recipients.py::recipients_for_station_alert\` — resolves \`alert.station\` to recipient queryset via topology queries.
- \`_send_email_notification\` now calls the helper. Routing: Vereins-Admin + Region-Manager (of station.region) + Station-Admin + Station-Maintainer. Excludes Vereins-Staff (operative role, not escalation inbox), Applicants, inactive, no-email users.
- \`send_test_notification("email")\` scoped to the requesting admin — fixes the multi-admin cross-notification annoyance.

**Phase 5 — Audit-log signals:**
- New \`apps/stations/signals.py\` with 7 handlers covering StationAssignment create/delete, Station.region change, RegionAssignment create/delete, Region CRUD.
- \`StationAuditLog.EventType\` extended with STATION_ASSIGNMENT_CREATED, STATION_ASSIGNMENT_REVOKED, STATION_REGION_CHANGED (code-only, no migration — choices aren't DB-enforced).
- \`apps/stations/apps.py\` gains \`ready()\` to register handlers at app startup.
- \`apps/audit/views.py\` merges AccountAuditLog as third source; \`_audit_table.html\` gets an \`account\` row variant.

**Phase 9 — Drop legacy groups:**
- Migration \`accounts/0007_drop_legacy_role_groups\` deletes \`admin\`/\`operator\`/\`member\` Django Groups. Reverse-code creates empty groups via \`get_or_create\` for 30-day backup-window rollback safety.

## What this PR does NOT do (deferred to PR-3)

- UI: User-Detail rollen-section, Station-Detail topology widgets, Region-CRUD admin page. Topology management still happens via Django Admin until PR-3.
- MEMBERSHIP_PROMOTED / MEMBERSHIP_DEMOTED audit-log emission. The event types live in \`AccountAuditLog.EventType\` from PR-1 but the emission point is the promote/demote view in PR-3 (signals lack actor context).
- Telegram per-user/per-station routing.
- Notification preferences per user.
- Renaming \`AdminOrOperatorMixin\` / \`AdminOrOperatorRequiredMixin\` mixins.

## Migrations

| # | Migration | What |
|---|---|---|
| accounts 0007 | drop_legacy_role_groups | data: delete admin/operator/member Groups (reverse: get_or_create) |

The 3 new StationAuditLog event types are code-only (TextChoices extension; not DB-enforced).

## Spec + Plan

- Spec: \`docs/superpowers/specs/2026-06-05-membership-levels-and-topology-roles-design.md\` (§4.6, §4.7)
- Plan: \`docs/superpowers/plans/2026-06-06-membership-levels-and-topology-roles-pr2.md\`

## Test plan

- [x] Full test suite green (~XYZ tests passing)
- [x] \`ruff format --check . && ruff check .\` clean
- [ ] Copilot review run
- [ ] Post-merge: \`gh workflow run main.yml --repo OE5XRX/servers\` to deploy
- [ ] Post-merge: trigger synthetic offline alert (stop a station's heartbeat >5min) and verify:
  - the new routing reaches the Station-Admin (assigned via Django Admin pre-PR-3) — currently still routes to Vereins-Admin without the assignment
  - email subject + body match the existing format
- [ ] Post-merge: verify Django Admin shows the legacy groups list is empty
- [ ] Begin PR-3 (UI for topology management) plan against the now-real signatures

## Execution notes

Built via Subagent-Driven Development per CLAUDE.md default.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Return the PR URL.**

---

## Self-Review

**Spec coverage:**

| Spec section | Implementing task |
|---|---|
| §4.6 AccountAuditLog merged into /audit/ | Task 5 |
| §4.7 Notification Routing (helper + dispatch + test-email) | Task 1 + Task 2 |
| §6 Migration 0009 (legacy group drop) | Task 6 (actual migration number: accounts 0007) |
| §6 Call-site refactor | Already done in PR-1 Task 10 |
| §7 Tests for notification routing | Task 1 (13 tests in tests/test_alert_recipients.py) |
| §7 Tests for audit emission | Task 4 + Task 5 (8 tests in tests/test_audit_log_emission.py) |
| §7 Tests for legacy-group drop | Task 6 (2 tests) |
| §8 Out-of-scope (UI, Telegram, etc.) | Honored — explicit "deferred to PR-3" callouts |
| §9 Known limitation: migration 0005 no audit | Honored — signals.py docstring + spec already say so |

**Placeholder scan:** None — every step shows exact code, exact paths, exact commands.

**Type / signature consistency:**

- `recipients_for_station_alert(station)` — defined in Task 1, consumed in Task 2.
- `_send_email_notification(alert, recipients_qs=None)` — defined in Task 2, no other consumer.
- `send_test_notification(channel, requesting_user=None)` — defined in Task 2 (notifications.py), consumed in Task 2 (views.py TestNotificationView).
- `_test_email(requesting_user=None)` — defined in Task 2.
- `StationAuditLog.EventType.STATION_ASSIGNMENT_{CREATED,REVOKED}` and `STATION_REGION_CHANGED` — defined in Task 3, consumed in Task 4 signals + Task 4 tests.
- `AccountAuditLog.EventType.REGION_ASSIGNMENT_{CREATED,REVOKED}` and `REGION_{CREATED,UPDATED,DELETED}` — defined in PR-1 Task 6, consumed in Task 4 signals + tests.
- `apps.stations.signals._on_*` — internal handlers, not consumed elsewhere; registered via `@receiver` decorators.
- `apps.audit.views.AuditLogListView.get_queryset` adds an `account_qs` block — uses `self.apply_sso_date_filters` (existing helper from the file) for consistency with the SSO branch (both have only date-based filters).
- Migration 0007 — referenced in Task 6 by both filename and dependency declaration; tests use migrator.apply_initial_migration with the exact name.

**No spec requirement without a task.** Plan complete.
