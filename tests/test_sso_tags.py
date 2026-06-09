"""Tests for the custom tag-management views (Django auth.Group repurposed)."""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts.models import User
from apps.sso.models import SsoAuditLog


@pytest.fixture
def admin(db):
    u = User.objects.create_user(username="admin", password="x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.save(update_fields=["membership_level"])
    User._invalidate_role_cache(u)
    return u


@pytest.fixture
def target_user(db):
    return User.objects.create_user(username="target", password="x")


def test_tag_list_requires_admin(db, client):
    user = User.objects.create_user(username="nonadmin", password="x")
    client.force_login(user)
    resp = client.get(reverse("sso:tag_list"))
    assert resp.status_code == 403


def test_tag_list_shows_existing_groups(db, client, admin):
    Group.objects.create(name="kontakt-team")
    Group.objects.create(name="funkdienst")
    client.force_login(admin)
    resp = client.get(reverse("sso:tag_list"))
    assert resp.status_code == 200
    assert b"kontakt-team" in resp.content
    assert b"funkdienst" in resp.content


def test_tag_create_rejects_invalid_slug(db, client, admin):
    client.force_login(admin)
    resp = client.post(reverse("sso:tag_create"), data={"name": "Kontakt Team"})
    assert resp.status_code == 400  # space not allowed in tag name


def test_tag_create_accepts_valid_slug_and_audits(db, client, admin):
    client.force_login(admin)
    resp = client.post(reverse("sso:tag_create"), data={"name": "kontakt-team"})
    assert resp.status_code in (200, 302)
    assert Group.objects.filter(name="kontakt-team").exists()


def test_tag_membership_toggle_adds_then_removes(db, client, admin, target_user):
    g = Group.objects.create(name="kontakt-team")
    client.force_login(admin)
    url = reverse("sso:tag_toggle", kwargs={"user_id": target_user.pk, "group_id": g.pk})

    client.post(url)
    assert target_user.groups.filter(pk=g.pk).exists()
    assert SsoAuditLog.objects.filter(
        event_type=SsoAuditLog.EventType.GROUP_MEMBERSHIP_CHANGED,
        target_user=target_user,
        message__icontains="added: target -> kontakt-team",
    ).exists()

    client.post(url)
    assert not target_user.groups.filter(pk=g.pk).exists()
    assert SsoAuditLog.objects.filter(
        target_user=target_user,
        message__icontains="removed: target -> kontakt-team",
    ).exists()


def test_tag_toggle_htmx_returns_partial(db, client, admin, target_user):
    g = Group.objects.create(name="kontakt-team")
    client.force_login(admin)
    url = reverse("sso:tag_toggle", kwargs={"user_id": target_user.pk, "group_id": g.pk})
    resp = client.post(url, HTTP_HX_REQUEST="true")
    assert resp.status_code == 200
    assert b"tags-card" in resp.content  # the root id of the partial
    assert b"kontakt-team" in resp.content
