import pytest

from apps.accounts.models import User


@pytest.mark.django_db
def test_notify_channel_defaults_to_email():
    u = User.objects.create_user(username="a", password="x", email="a@x")
    assert u.notify_channel == User.NotifyChannel.EMAIL


@pytest.mark.django_db
def test_notify_channel_choices():
    assert {c[0] for c in User.NotifyChannel.choices} == {"email", "push", "both"}
