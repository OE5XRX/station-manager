import pytest

from apps.accounts.models import User


@pytest.mark.django_db
def test_base_head_has_manifest_and_sw_registration(client):
    # /accounts/login/ is a standalone template (not base.html); use the
    # dashboard which extends base.html. LoginRequiredMiddleware is active so
    # we force_login a minimal user.
    u = User.objects.create_user(username="pwa-test", password="x", email="pwa@x")
    client.force_login(u)
    r = client.get("/", follow=True)
    html = r.content.decode()
    assert 'rel="manifest"' in html
    assert "/manifest.webmanifest" in html
    assert "serviceWorker" in html
    assert 'name="apple-mobile-web-app-capable"' in html
    # favicon is the official OE5XRX logo (favicon.ico), not the old inline
    # data-URI placeholder
    assert "favicon.ico" in html
    assert "data:image/svg+xml" not in html
