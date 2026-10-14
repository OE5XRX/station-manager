import pytest
from django.core.cache import cache
from django.test import RequestFactory

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.permissions import AreaDenied, Level, require

DENIED = AccountAuditLog.EventType.PERMISSION_DENIED


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def member(db):
    return User.objects.create_user(username="mem", password="x", membership_level="member")


def test_require_passes_silently(member):
    require(member, "control", "use")
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_require_raises_with_levels_and_logs(member):
    req = RequestFactory().post("/de/images/1/archive/", REMOTE_ADDR="10.0.0.9")
    req.user = member
    with pytest.raises(AreaDenied) as ei:
        require(member, "images", "manage", request=req)
    exc = ei.value
    assert (exc.area, exc.verb) == ("images", "manage")
    assert exc.required_level == Level.ADMIN and exc.actual_level == Level.MEMBER
    assert "Vereins-Admin" in str(exc) and "Vereins-Mitglied" in str(exc)
    row = AccountAuditLog.objects.get(event_type=DENIED)
    assert row.actor == member
    assert row.ip_address == "10.0.0.9"
    assert row.details == {
        "area": "images",
        "verb": "manage",
        "path": "/de/images/1/archive/",
        "method": "POST",
        "channel": "web",
        "required_level": "admin",
        "actual_level": "member",
        "count": 1,
    }


def test_denial_dedupe_increments_count(member):
    for _ in range(3):
        with pytest.raises(AreaDenied):
            require(member, "audit", "view", path="/de/audit/", method="GET")
    row = AccountAuditLog.objects.get(event_type=DENIED)
    assert row.details["count"] == 3


def test_dedupe_is_per_path_and_verb(member):
    for path in ("/a/", "/b/"):
        with pytest.raises(AreaDenied):
            require(member, "audit", "view", path=path, method="GET")
    assert AccountAuditLog.objects.filter(event_type=DENIED).count() == 2


def test_dedupe_survives_deleted_row(member):
    with pytest.raises(AreaDenied):
        require(member, "audit", "view", path="/x/", method="GET")
    AccountAuditLog.objects.filter(event_type=DENIED).delete()
    with pytest.raises(AreaDenied):
        require(member, "audit", "view", path="/x/", method="GET")
    assert AccountAuditLog.objects.get(event_type=DENIED).details["count"] == 1


def test_anonymous_raises_but_is_not_logged(db):
    from django.contrib.auth.models import AnonymousUser

    with pytest.raises(AreaDenied):
        require(AnonymousUser(), "dashboard", "view", path="/", method="GET")
    assert not AccountAuditLog.objects.filter(event_type=DENIED).exists()


def test_api_body_shape(member):
    with pytest.raises(AreaDenied) as ei:
        require(
            member,
            "images",
            "manage",
            path="/api/v1/images/1/archive/",
            method="POST",
            channel="api",
        )
    body = ei.value.as_api_body()
    assert body["required"] == "images.manage"
    assert body["required_level"] == "admin"
    assert body["your_level"] == "member"
    assert body["detail"]


def test_nobody_verb_message(member):
    with pytest.raises(AreaDenied) as ei:
        require(member, "audit", "manage", path="/x/", method="POST")
    assert ei.value.required_level is None
    assert ei.value.as_api_body()["required_level"] is None
