# tests/test_webpush_subscribe_api.py
import json

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.webpush.models import PushSubscription

SUB = {"endpoint": "https://push.example/z", "keys": {"p256dh": "pp", "auth": "aa"}}


@pytest.mark.django_db
def test_subscribe_requires_login(client):
    r = client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert r.status_code in (302, 403)


@pytest.mark.django_db
def test_subscribe_creates_then_upserts(client):
    u = User.objects.create_user(username="a", password="x", email="a@x")
    client.force_login(u)
    r = client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert PushSubscription.objects.filter(user=u, endpoint=SUB["endpoint"]).count() == 1
    # second POST same endpoint → update, not duplicate
    client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert PushSubscription.objects.filter(endpoint=SUB["endpoint"]).count() == 1


@pytest.mark.django_db
def test_subscribe_rejects_cross_user_endpoint(client):
    owner = User.objects.create_user(username="o2", password="x", email="o2@x")
    other = User.objects.create_user(username="p2", password="x", email="p2@x")
    PushSubscription.objects.create(user=owner, endpoint=SUB["endpoint"], p256dh="pp", auth="aa")
    client.force_login(other)
    r = client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert r.status_code == 409
    # ownership must NOT transfer to the caller
    s = PushSubscription.objects.get(endpoint=SUB["endpoint"])
    assert s.user_id == owner.id


@pytest.mark.django_db
def test_unsubscribe_only_removes_own(client):
    owner = User.objects.create_user(username="o", password="x", email="o@x")
    other = User.objects.create_user(username="p", password="x", email="p@x")
    PushSubscription.objects.create(user=owner, endpoint=SUB["endpoint"], p256dh="pp", auth="aa")
    client.force_login(other)
    r = client.post(
        reverse("webpush:unsubscribe"),
        data=json.dumps({"endpoint": SUB["endpoint"]}),
        content_type="application/json",
    )
    assert r.status_code == 200
    # other user's request must NOT delete owner's subscription
    assert PushSubscription.objects.filter(endpoint=SUB["endpoint"]).exists()
