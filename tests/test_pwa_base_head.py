import re

import pytest

from apps.accounts.models import User


def _icon_link(html):
    """Return the <link rel="icon" ...> tag from a rendered page, or ""."""
    m = re.search(r'<link\b[^>]*\brel="icon"[^>]*>', html)
    return m.group(0) if m else ""


@pytest.mark.django_db
def test_base_head_has_manifest_and_sw_registration(client):
    # /accounts/login/ is a standalone template (not base.html); use the
    # dashboard which extends base.html. LoginRequiredMiddleware is active so
    # we force_login a minimal user.
    u = User.objects.create_user(username="pwa-test", password="x", email="pwa@x")
    client.force_login(u)
    r = client.get("/", follow=True)
    assert r.status_code == 200
    html = r.content.decode()
    assert 'rel="manifest"' in html
    assert "/manifest.webmanifest" in html
    assert "serviceWorker" in html
    assert 'name="apple-mobile-web-app-capable"' in html
    # favicon is the official OE5XRX logo (favicon.ico), asserted on the actual
    # <link rel="icon"> tag (not a bare page-wide substring), and the old inline
    # "5X" data-URI placeholder must be gone.
    assert "favicon.ico" in _icon_link(html)
    assert "5X%3C/text%3E" not in html


@pytest.mark.django_db
def test_login_page_uses_official_favicon(client):
    # The login page is a standalone template (does NOT extend base.html), so
    # it carries its own favicon link — it must also use the official favicon,
    # not the old inline placeholder. follow=True: /accounts/login/ i18n-
    # redirects to the locale-prefixed URL.
    r = client.get("/accounts/login/", follow=True)
    assert r.status_code == 200
    html = r.content.decode()
    assert "favicon.ico" in _icon_link(html)
    assert "5X%3C/text%3E" not in html
