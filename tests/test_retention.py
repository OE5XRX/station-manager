"""Retention engine (apps/audit/retention.py): Löschfristen 365 d / IPs 90 d."""

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application, RefreshToken

from apps.accounts.models import AccountAuditLog, AccountToken, User
from apps.audit import retention
from apps.audit.retention import RetentionConfig, run_retention
from apps.sso.models import SsoAuditLog, TokenSession
from apps.stations.models import StationAuditLog
from apps.tunnel.models import TerminalSession

NOW = timezone.now()
E = StationAuditLog.EventType

CATEGORIES = [
    "station_audit_deleted",
    "account_audit_deleted",
    "sso_audit_deleted",
    "terminal_sessions_deleted",
    "station_audit_ip_cleared",
    "account_audit_ip_cleared",
    "sso_audit_ip_cleared",
    "station_audit_ip_scrubbed",
    "token_session_client_cleared",
    "account_token_ip_cleared",
    "account_tokens_expired_deleted",
    "token_sessions_pruned",
    "django_sessions_cleared",
    "oauth_tokens_cleared",
    "audit_messages_scrubbed",
]


def _cfg(enforce=True, now=NOW, batch_size=1000):
    return RetentionConfig(
        now=now, audit_days=365, ip_days=90, enforce=enforce, batch_size=batch_size
    )


def _age(obj, days, field="created_at"):
    type(obj).objects.filter(pk=obj.pk).update(**{field: NOW - timedelta(days=days)})
    return obj


def _exists(obj):
    return type(obj).objects.filter(pk=obj.pk).exists()


def _fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


@pytest.fixture
def user(db):
    return User.objects.create_user(username="OE5MEM", email="m@example.org", password="x")


@pytest.fixture
def other(db):
    return User.objects.create_user(username="OE5OTH", email="o@example.org", password="x")


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/cb/",
    )


def _station_row(
    station, days, event_type=E.UPDATED, user=None, ip=None, message="m", changes=None
):
    row = StationAuditLog.objects.create(
        station=station,
        event_type=event_type,
        user=user,
        ip_address=ip,
        message=message,
        changes=changes or {},
    )
    return _age(row, days)


def _account_row(days, actor=None, target=None, ip=None, message="m"):
    row = AccountAuditLog.objects.create(
        event_type=AccountAuditLog.EventType.USER_UPDATED,
        actor=actor,
        target_user=target,
        ip_address=ip,
        message=message,
    )
    return _age(row, days)


def _sso_row(days, actor=None, target=None, ip=None, message="m"):
    row = SsoAuditLog.objects.create(
        event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
        actor=actor,
        target_user=target,
        ip_address=ip,
        message=message,
    )
    return _age(row, days)


def test_config_from_settings(settings):
    settings.RETENTION_AUDIT_DAYS = 0
    settings.RETENTION_IP_DAYS = -5
    settings.RETENTION_ENFORCE = True
    settings.RETENTION_BATCH_SIZE = 50
    cfg = RetentionConfig.from_settings(now=NOW)
    assert (cfg.audit_days, cfg.ip_days, cfg.enforce, cfg.batch_size) == (1, 1, True, 50)
    assert cfg.audit_cutoff == NOW - timedelta(days=1)
    assert cfg.ip_cutoff == NOW - timedelta(days=1)
    assert RetentionConfig.from_settings(now=NOW, dry_run=True).enforce is False


def test_enforce_false_setting_forces_dry_run(settings):
    settings.RETENTION_ENFORCE = False
    assert RetentionConfig.from_settings().enforce is False


def test_dry_run_counts_but_changes_nothing(station, user):
    row = _station_row(station, 366, E.CONTROL_PTT, user=user, ip="192.0.2.1")
    result = run_retention(_cfg(enforce=False))
    assert result.enforce is False
    assert result.errors == []
    assert list(result.counts) == CATEGORIES
    assert result.counts["station_audit_deleted"] == 1
    assert result.counts["station_audit_ip_cleared"] >= 1
    assert _fresh(row).ip_address == "192.0.2.1"


def test_station_audit_deletes_only_personal_rows(station, user):
    personal = _station_row(station, 366, E.UPDATED, user=user)
    purged_user_ptt = _station_row(station, 366, E.CONTROL_PTT, user=None)
    system = _station_row(station, 366, E.STATUS_CHANGE, user=None)
    recent = _station_row(station, 364, E.UPDATED, user=user)
    result = run_retention(_cfg())
    assert result.counts["station_audit_deleted"] == 2
    assert not _exists(personal)
    assert not _exists(purged_user_ptt)
    assert _exists(system)
    assert _exists(recent)


def test_account_and_sso_audit_deleted_after_365_days(user):
    old_acc = _account_row(366, actor=user)
    old_acc_system = _account_row(366)
    new_acc = _account_row(364, target=user)
    old_sso = _sso_row(366, target=user)
    new_sso = _sso_row(364, target=user)
    result = run_retention(_cfg())
    assert result.counts["account_audit_deleted"] == 2
    assert result.counts["sso_audit_deleted"] == 1
    assert not _exists(old_acc) and not _exists(old_acc_system) and not _exists(old_sso)
    assert _exists(new_acc) and _exists(new_sso)


def test_terminal_sessions_closed_and_orphaned_deleted(station, user):
    def make(ended_days=None, started_days=0):
        s = TerminalSession.objects.create(station=station, user=user)
        TerminalSession.objects.filter(pk=s.pk).update(
            started_at=NOW - timedelta(days=started_days),
            ended_at=None if ended_days is None else NOW - timedelta(days=ended_days),
        )
        return s

    closed_old = make(ended_days=366, started_days=366)
    closed_recent = make(ended_days=10, started_days=10)
    orphan = make(ended_days=None, started_days=400)
    active = make(ended_days=None, started_days=0)
    result = run_retention(_cfg())
    assert result.counts["terminal_sessions_deleted"] == 2
    assert not _exists(closed_old) and not _exists(orphan)
    assert _exists(closed_recent) and _exists(active)


def test_ip_cleared_after_90_days_row_kept(station, user):
    rows = [
        _station_row(station, 91, E.UPDATED, user=user, ip="192.0.2.1", message="kept"),
        _account_row(91, actor=user, ip="192.0.2.1", message="kept"),
        _sso_row(91, target=user, ip="192.0.2.1", message="kept"),
    ]
    recent = [
        _station_row(station, 89, E.UPDATED, user=user, ip="192.0.2.2"),
        _account_row(89, actor=user, ip="192.0.2.2"),
        _sso_row(89, target=user, ip="192.0.2.2"),
    ]
    result = run_retention(_cfg())
    for key in ("station_audit_ip_cleared", "account_audit_ip_cleared", "sso_audit_ip_cleared"):
        assert result.counts[key] == 1
    for row in rows:
        fresh = _fresh(row)
        assert fresh.ip_address is None
        assert fresh.message == "kept"
    for row in recent:
        assert _fresh(row).ip_address == "192.0.2.2"


def test_station_ip_change_message_scrubbed_after_90_days(station):
    def ip_change(days):
        return _station_row(
            station,
            days,
            E.HEARTBEAT,
            message="IP address changed: 10.0.0.1 → 10.0.0.2",
            changes={"ip_address": {"old": "10.0.0.1", "new": "10.0.0.2"}},
        )

    old = ip_change(91)
    recent = ip_change(89)
    result = run_retention(_cfg())
    assert result.counts["station_audit_ip_scrubbed"] == 1
    fresh = _fresh(old)
    assert fresh.user is None  # system row: scrubbed regardless of user
    assert fresh.message == "IP address changed: [ip] → [ip]"
    assert fresh.changes == {"ip_address": {"old": "[ip]", "new": "[ip]"}}
    untouched = _fresh(recent)
    assert untouched.message == "IP address changed: 10.0.0.1 → 10.0.0.2"
    assert untouched.changes == {"ip_address": {"old": "10.0.0.1", "new": "10.0.0.2"}}

    assert run_retention(_cfg()).counts["station_audit_ip_scrubbed"] == 0


def test_station_scrub_covers_changes_only_and_user_rows(station, user):
    changes_only = _station_row(
        station,
        91,
        E.UPDATED,
        user=user,
        message="Station updated",
        changes={"contact": {"old": "a@b.at", "new": ["x", {"ip": "2001:db8::1"}]}, "n": 3},
    )
    harmless = _station_row(
        station, 91, E.FIRMWARE_UPDATE, message="firmware v1.2.3.4 at 12:30:45"
    )
    result = run_retention(_cfg())
    assert result.counts["station_audit_ip_scrubbed"] == 1
    fresh = _fresh(changes_only)
    assert fresh.message == "Station updated"
    assert fresh.changes == {"contact": {"old": "[email]", "new": ["x", {"ip": "[ip]"}]}, "n": 3}
    assert _fresh(harmless).message == "firmware v1.2.3.4 at 12:30:45"


def test_station_scrub_dry_run_counts_only(station):
    row = _station_row(station, 91, E.HEARTBEAT, message="IP address changed: 10.0.0.1 → 10.0.0.2")
    result = run_retention(_cfg(enforce=False))
    assert result.counts["station_audit_ip_scrubbed"] == 1
    assert _fresh(row).message == "IP address changed: 10.0.0.1 → 10.0.0.2"


def test_token_session_client_fields_cleared(user, app):
    def make(days):
        s = TokenSession.objects.create(
            user=user,
            application=app,
            ip_address="192.0.2.1",
            user_agent="UA",
            country_code="AT",
            city="Linz",
        )
        return _age(s, days, field="issued_at")

    old = make(91)
    recent = make(10)
    result = run_retention(_cfg(enforce=True))
    assert result.counts["token_session_client_cleared"] >= 1
    if _exists(old):  # may also be pruned by the stale rule — then it is gone anyway
        fresh = _fresh(old)
        assert (fresh.ip_address, fresh.user_agent, fresh.country_code, fresh.city) == (
            None,
            "",
            "",
            "",
        )
    assert _fresh(recent).city == "Linz"


def test_token_session_client_fields_cleared_on_kept_session(user, app):
    # Dry-run isolates the clear step from the prune step (stale after 14 + 30 d).
    s = TokenSession.objects.create(user=user, application=app, city="Linz")
    _age(s, 91, field="issued_at")
    result = run_retention(_cfg(enforce=False))
    assert result.counts["token_session_client_cleared"] == 1
    assert result.counts["token_sessions_pruned"] == 1


def test_account_token_ip_cleared_and_expired_deleted(user):
    def make(created_days, expires_in_days):
        t = AccountToken.objects.create(
            user=user,
            token_type=AccountToken.TokenType.RESET,
            secret_hash=f"h{created_days}{expires_in_days}",
            expires_at=NOW + timedelta(days=expires_in_days),
            ip_created="192.0.2.1",
        )
        return _age(t, created_days)

    old_ip_valid = make(91, 5)
    expired = make(1, -1)
    fresh_ip = make(1, 5)
    result = run_retention(_cfg())
    assert result.counts["account_token_ip_cleared"] == 1
    assert result.counts["account_tokens_expired_deleted"] == 1
    assert _fresh(old_ip_valid).ip_created is None
    assert not _exists(expired)
    assert _fresh(fresh_ip).ip_created == "192.0.2.1"


def test_prune_token_sessions_reused(user, app):
    keep = TokenSession.objects.create(user=user, application=app)
    stale = TokenSession.objects.create(user=user, application=app)
    TokenSession.objects.filter(pk=stale.pk).update(revoked_at=NOW - timedelta(days=40))
    assert list(TokenSession.objects.stale(NOW)) == [_fresh(stale)]
    out = StringIO()
    call_command("prune_token_sessions", stdout=out)
    assert out.getvalue().strip() == "Pruned 1 TokenSession row(s)."
    assert _exists(keep) and not _exists(stale)

    stale2 = TokenSession.objects.create(user=user, application=app)
    _age(stale2, 60, field="issued_at")
    result = run_retention(_cfg())
    assert result.counts["token_sessions_pruned"] == 1
    assert not _exists(stale2) and _exists(keep)


def test_expired_django_sessions_cleared(db):
    Session.objects.create(
        session_key="old", session_data="x", expire_date=NOW - timedelta(days=1)
    )
    Session.objects.create(
        session_key="new", session_data="x", expire_date=NOW + timedelta(days=1)
    )
    assert run_retention(_cfg(enforce=False)).counts["django_sessions_cleared"] == 1
    assert Session.objects.count() == 2
    assert run_retention(_cfg()).counts["django_sessions_cleared"] == 1
    assert list(Session.objects.values_list("session_key", flat=True)) == ["new"]


def test_expired_oauth_tokens_counted_and_cleared(user, app):
    AccessToken.objects.create(
        user=user, application=app, token="old", expires=NOW - timedelta(days=1), scope="openid"
    )
    live = AccessToken.objects.create(
        user=user, application=app, token="live", expires=NOW + timedelta(days=1), scope="openid"
    )
    RefreshToken.objects.create(user=user, application=app, token="r-live", access_token=live)
    assert run_retention(_cfg(enforce=False)).counts["oauth_tokens_cleared"] == 1
    result = run_retention(_cfg())
    assert result.counts["oauth_tokens_cleared"] == 1
    assert result.errors == []
    assert AccessToken.objects.filter(token="live").exists()


def test_oauth_cleanup_keeps_rotated_sessions_in_forensic_window(user, app):
    """Unlike DOT's cleartokens, a revoked refresh token whose TokenSession is
    still inside the 30-day window survives (the session would cascade away)."""
    rotated_rt = RefreshToken.objects.create(
        user=user, application=app, token="r-rotated", revoked=NOW - timedelta(days=1)
    )
    session = TokenSession.objects.create(user=user, application=app, refresh_token=rotated_rt)
    orphan_rt = RefreshToken.objects.create(
        user=user, application=app, token="r-orphan", revoked=NOW - timedelta(days=1)
    )
    live_rt = RefreshToken.objects.create(user=user, application=app, token="r-live")
    result = run_retention(_cfg())
    assert result.errors == []
    assert _exists(session) and _exists(rotated_rt) and _exists(live_rt)
    assert not _exists(orphan_rt)


def test_oauth_cleanup_skips_held_users(user, app, monkeypatch):
    monkeypatch.setattr(retention, "held_user_ids", lambda: frozenset({user.pk}))
    AccessToken.objects.create(
        user=user, application=app, token="old", expires=NOW - timedelta(days=1), scope="openid"
    )
    assert run_retention(_cfg()).counts["oauth_tokens_cleared"] == 0
    assert AccessToken.objects.filter(token="old").exists()


def test_audit_messages_scrubbed_and_idempotent(user):
    legacy = _account_row(5, target=user, message="to a@b.at from 192.0.2.1")
    sso = _sso_row(5, target=user, message="Token issued. UA=Mozilla/5.0 City=Linz")
    clean = _account_row(5, target=user, message="reset link sent from the form")
    result = run_retention(_cfg())
    assert result.counts["audit_messages_scrubbed"] == 2
    assert _fresh(legacy).message == "to [email] from [ip]"
    assert _fresh(sso).message == "Token issued."
    assert _fresh(clean).message == "reset link sent from the form"
    assert run_retention(_cfg()).counts["audit_messages_scrubbed"] == 0


def test_held_users_are_skipped(monkeypatch, station, user, other):
    monkeypatch.setattr(retention, "held_user_ids", lambda: frozenset({user.pk}))
    held_rows = [
        _station_row(station, 366, E.CONTROL_PTT, user=user, ip="192.0.2.1"),
        _account_row(366, actor=user, ip="192.0.2.1"),
        _account_row(366, target=user, ip="192.0.2.1"),
        _sso_row(366, target=user, ip="192.0.2.1"),
        _station_row(station, 91, E.UPDATED, user=user, message="from 10.0.0.1", ip="192.0.2.1"),
    ]
    other_rows = [
        _station_row(station, 366, E.CONTROL_PTT, user=other),
        _account_row(366, actor=other),
        _sso_row(366, target=other),
    ]
    other_ip = _account_row(91, actor=other, ip="192.0.2.9")
    result = run_retention(_cfg())
    assert result.errors == []
    for row in held_rows:
        fresh = _fresh(row)
        assert fresh.ip_address == "192.0.2.1"
    assert _fresh(held_rows[-1]).message == "from 10.0.0.1"
    for row in other_rows:
        assert not _exists(row)
    assert _fresh(other_ip).ip_address is None


def test_run_is_idempotent(station, user, app):
    _station_row(station, 366, E.CONTROL_PTT, user=user, ip="192.0.2.1")
    _station_row(station, 91, E.HEARTBEAT, message="IP address changed: 10.0.0.1 → 10.0.0.2")
    _account_row(91, actor=user, ip="192.0.2.1", message="to a@b.at")
    _sso_row(366, target=user)
    s = TokenSession.objects.create(user=user, application=app, city="Linz")
    _age(s, 91, field="issued_at")
    first = run_retention(_cfg())
    assert any(first.counts.values())
    second = run_retention(_cfg())
    assert second.errors == []
    assert set(second.counts.values()) == {0}, second.counts


def test_batching(station, user):
    rows = [_station_row(station, 366, E.CONTROL_PTT, user=user, ip="192.0.2.1") for _ in range(5)]
    msgs = [_account_row(1, message=f"to u{i}@example.org") for i in range(5)]
    result = run_retention(_cfg(batch_size=2))
    assert result.counts["station_audit_deleted"] == 5
    assert result.counts["audit_messages_scrubbed"] == 5
    assert not any(_exists(r) for r in rows)
    assert all(_fresh(m).message == "to [email]" for m in msgs)


def test_step_error_is_isolated(monkeypatch, station, user):
    old = _station_row(station, 366, E.CONTROL_PTT, user=user)

    def boom(cfg, held):
        raise RuntimeError("boom")

    monkeypatch.setattr(retention, "_account_audit_deleted", boom)
    result = run_retention(_cfg())
    assert result.errors == ["account_audit_deleted"]
    assert "account_audit_deleted" not in result.counts
    assert result.counts["station_audit_deleted"] == 1
    assert result.counts["sso_audit_deleted"] == 0
    assert not _exists(old)


def test_personal_station_event_types_are_valid():
    assert retention.PERSONAL_STATION_EVENT_TYPES <= set(E.values)
    assert len(retention.PERSONAL_STATION_EVENT_TYPES) == 8
