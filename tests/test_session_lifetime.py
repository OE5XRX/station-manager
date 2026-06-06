"""Session-cookie lifetime behavior.

Verifies the "rolling 7-day session" contract:

* ``SESSION_COOKIE_AGE = 604800`` (7 days) — the cookie max-age.
* ``SESSION_SAVE_EVERY_REQUEST = True`` — every response refreshes the
  sessionid cookie, so the 7-day window slides forward as long as the
  operator is actively using the UI.

Together they replace the previous fixed-from-login 8-hour session
that kicked everyone out roughly once per working day even with the
tab still open.
"""

import pytest
from django.urls import reverse

SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60


@pytest.mark.django_db
def test_login_sets_sessionid_cookie_with_seven_day_max_age(client, admin_user):
    """Logging in via the form sets sessionid with a 7-day max-age."""
    response = client.post(
        reverse("accounts:login"),
        {"username": "admin", "password": "testpass123"},
    )
    assert response.status_code == 302
    assert "sessionid" in response.cookies
    assert response.cookies["sessionid"]["max-age"] == SEVEN_DAYS_SECONDS


@pytest.mark.django_db
def test_authenticated_request_refreshes_sessionid_cookie(client, admin_user):
    """A request that does NOT modify the session still re-sends the
    sessionid cookie with a fresh 7-day max-age.

    This is the SAVE_EVERY_REQUEST=True behavior — without it, the
    session expires 7 days after login regardless of activity. With
    it, the cookie expiry "rolls" forward on every request, so the
    user only gets logged out after 7 days of true inactivity.
    """
    client.force_login(admin_user)

    # GET a page that does no session writes of its own.
    response = client.get(reverse("dashboard:index"))
    assert response.status_code == 200
    assert "sessionid" in response.cookies, (
        "SAVE_EVERY_REQUEST must re-set the sessionid cookie even when "
        "the view did not touch request.session."
    )
    assert response.cookies["sessionid"]["max-age"] == SEVEN_DAYS_SECONDS
