"""Django admin: all level fields are read-only in UserAdmin. is_staff/is_superuser
are derived from membership_level, and membership_level itself changes only via the
app's membership UI/API (audit MEMBERSHIP_PROMOTED/DEMOTED, self-change block,
assignment guard) — the admin must not bypass those."""

import datetime

import pytest
from django.urls import reverse

from apps.accounts.models import User

pytestmark = pytest.mark.django_db


def _change_url(user):
    return reverse("admin:accounts_user_change", args=[user.pk])


def test_level_fields_all_read_only(client, admin_user):
    target = User.objects.create_user(username="adm-t", password="x", membership_level="member")
    client.force_login(admin_user)
    r = client.get(_change_url(target))
    assert r.status_code == 200
    form_fields = r.context["adminform"].form.fields
    assert "membership_level" not in form_fields
    assert "is_staff" not in form_fields
    assert "is_superuser" not in form_fields
    html = r.content.decode()
    assert 'name="membership_level"' not in html
    assert 'name="is_staff"' not in html and 'name="is_superuser"' not in html


def test_admin_cannot_change_membership_level(client, admin_user):
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
    data["membership_level"] = "admin"  # forged: read-only, ignored
    data["is_staff"] = "on"  # forged: read-only, ignored
    data.pop("password", None)
    data.pop("avatar", None)
    resp = client.post(_change_url(target), data)
    assert resp.status_code == 302, (
        getattr(resp, "context", None) and resp.context["adminform"].form.errors
    )
    target.refresh_from_db()
    assert target.membership_level == "member"
    assert target.is_staff is False and target.is_superuser is False
