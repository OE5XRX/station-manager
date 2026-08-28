import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.mark.django_db
def test_page_requires_login(client):
    r = client.get(reverse("accounts:notification_settings"))
    assert r.status_code == 302


@pytest.mark.django_db
def test_post_updates_channel(client):
    u = User.objects.create_user(username="a", password="x", email="a@x")
    client.force_login(u)
    r = client.post(
        reverse("accounts:notification_settings"), data={"notify_channel": "both"}
    )
    assert r.status_code in (200, 302)
    u.refresh_from_db()
    assert u.notify_channel == User.NotifyChannel.BOTH
