"""Django admin: is_staff/is_superuser are derived from membership_level, so they
are read-only in UserAdmin and membership_level is the editable field."""

import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _change_url(user):
    return reverse("admin:accounts_user_change", args=[user.pk])


def test_membership_level_editable_and_flags_read_only(client, admin_user):
    target = User.objects.create_user(username="adm-t", password="x", membership_level="member")
    client.force_login(admin_user)
    r = client.get(_change_url(target))
    assert r.status_code == 200
    form_fields = r.context["adminform"].form.fields
    assert "membership_level" in form_fields
    assert "is_staff" not in form_fields
    assert "is_superuser" not in form_fields
    html = r.content.decode()
    assert 'name="membership_level"' in html
    assert 'name="is_staff"' not in html and 'name="is_superuser"' not in html


def test_admin_promotes_via_membership_level(client, admin_user):
    target = User.objects.create_user(username="adm-p", password="x", membership_level="member")
    client.force_login(admin_user)
    r = client.get(_change_url(target))
    data = {}
    form = r.context["adminform"].form
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if value is None:
            continue
        if isinstance(value, datetime.datetime):
            data[f"{name}_0"] = value.date().isoformat()
            data[f"{name}_1"] = value.time().strftime("%H:%M:%S")
        elif isinstance(value, (list, tuple)):
            data[name] = [getattr(v, "pk", v) for v in value]
        else:
            data[name] = getattr(value, "pk", value)
    data["membership_level"] = "admin"
    data["is_staff"] = ""  # a forged flag is ignored (read-only)
    data.pop("password", None)
    data.pop("avatar", None)
    resp = client.post(_change_url(target), data)
    assert resp.status_code == 302, (
        getattr(resp, "context", None) and resp.context["adminform"].form.errors
    )
    target.refresh_from_db()
    assert target.membership_level == "admin"
    assert target.is_staff is True and target.is_superuser is True
