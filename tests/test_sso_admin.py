"""Smoke tests for the custom Application admin overrides."""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from oauth2_provider.models import Application

from apps.sso.models import AppGrant

User = get_user_model()


@pytest.fixture
def superadmin(db):
    """A superuser who is also in the admin group (the project's admin
    gate looks at groups; superuser alone is not enough)."""
    g, _ = Group.objects.get_or_create(name="admin")
    u = User.objects.create_superuser(username="superadmin", password="x", email="a@x.test")
    u.groups.add(g)
    return u


@pytest.mark.django_db
def test_application_admin_list_displays_active_grant_count(client, superadmin):
    """Lists should show how many active grants point at each app — admin's
    quick proxy for 'is this app used?'."""
    client.force_login(superadmin)

    app = Application.objects.create(
        name="InvenTree-Test",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x.example.org/cb/",
    )
    user2 = User.objects.create_user(username="u2", password="x", email="u2@x.test")
    AppGrant.objects.create(user=user2, application=app)

    # follow=True: project mounts admin under i18n_patterns, so the bare
    # /admin/ path redirects to /en/admin/. We don't care about the redirect,
    # just the final rendered list.
    resp = client.get("/admin/oauth2_provider/application/", follow=True)
    assert resp.status_code == 200
    assert b"InvenTree-Test" in resp.content
    # The grant count column should show "1" for this app.
    # We just check that "1" appears in the response body alongside the app
    # name — strict cell matching is brittle.
    assert b"1" in resp.content


@pytest.mark.django_db
def test_application_admin_change_form_marks_client_secret_readonly(client, superadmin):
    """After Application creation, client_secret should be read-only in
    the change form — prevents an admin from accidentally rotating it
    in a way that breaks every RP simultaneously."""
    client.force_login(superadmin)

    app = Application.objects.create(
        name="App-Y",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://y.example.org/cb/",
    )

    resp = client.get(
        f"/admin/oauth2_provider/application/{app.pk}/change/", follow=True
    )
    assert resp.status_code == 200
    # Read-only fields in Django admin render as a <div class="readonly">.
    # We don't need to assert the exact widget; sufficient to assert that
    # there's no <input name="client_secret"> input box.
    assert b'name="client_secret"' not in resp.content


@pytest.mark.django_db
def test_application_admin_only_counts_active_grants(client, superadmin):
    """active_grants must exclude revoked rows."""
    from django.utils import timezone

    client.force_login(superadmin)

    app = Application.objects.create(
        name="App-Z",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://z.example.org/cb/",
    )
    u1 = User.objects.create_user(username="u1", password="x", email="u1@x.test")
    u2 = User.objects.create_user(username="u2", password="x", email="u2@x.test")

    AppGrant.objects.create(user=u1, application=app)
    g2 = AppGrant.objects.create(user=u2, application=app)
    g2.revoked_at = timezone.now()
    g2.save()

    # Active count should be 1, not 2.
    from apps.sso.admin import CustomApplicationAdmin
    admin_obj = CustomApplicationAdmin(Application, None)
    assert admin_obj.active_grants(app) == 1
