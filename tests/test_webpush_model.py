import pytest
from django.db import IntegrityError

from apps.accounts.models import User
from apps.webpush.models import PushSubscription


def _sub(user, endpoint="https://push.example/abc"):
    return PushSubscription.objects.create(user=user, endpoint=endpoint, p256dh="p", auth="a")


@pytest.mark.django_db
def test_subscription_created_with_defaults():
    u = User.objects.create_user(username="a", password="x", email="a@x")
    s = _sub(u)
    assert s.failure_count == 0
    assert s.last_success_at is None
    assert list(u.push_subscriptions.all()) == [s]


@pytest.mark.django_db
def test_endpoint_is_unique():
    u = User.objects.create_user(username="b", password="x", email="b@x")
    _sub(u)
    with pytest.raises(IntegrityError):
        _sub(u)


@pytest.mark.django_db
def test_subscription_info_shape():
    u = User.objects.create_user(username="c", password="x", email="c@x")
    sub = PushSubscription.objects.create(
        user=u,
        endpoint="https://push.example/abc",
        p256dh="dGVzdC1wMjU2ZGg=",
        auth="dGVzdC1hdXRo",
    )
    assert sub.subscription_info == {
        "endpoint": "https://push.example/abc",
        "keys": {
            "p256dh": "dGVzdC1wMjU2ZGg=",
            "auth": "dGVzdC1hdXRo",
        },
    }
