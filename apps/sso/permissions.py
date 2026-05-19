"""OIDC access-control: bridges AppGrant into DOT's authorization flow.

`user_can_access` is the pure-function gate used by the validator
class below. Tests target this function directly; the validator
class is the integration point with DOT.

Integration strategy:
DOT's OAuth2Validator has several methods called during the
authorization-code flow. The cleanest override for "is this user
allowed to use this client at all?" is `validate_user` for
password-grant, plus a custom check in our project-level
authorization-view wrapper for authorization-code grant.

For T9 we ship:
- `user_can_access(user, application)` — pure function, testable.
- `SsoOAuth2Validator` — DOT subclass with PKCE enforcement.
- `validate_user` overridden to consult AppGrant (covers the
  password-grant code path, defense-in-depth — we don't use that
  grant type but a misconfiguration shouldn't bypass the gate).

The authorization-code path is gated by middleware/views in T13–T15.
Integration is end-to-end-tested in test_sso_flow.py (T18).

DOT version pin (verified against the installed source under
/usr/local/lib/python3.14/site-packages/oauth2_provider/oauth2_validators.py):

- ``validate_user(self, username, password, client, request, *args, **kwargs)``
- ``is_pkce_required(self, client_id, request)``  — note the trailing
  ``request`` arg; older DOT releases only took ``client_id``.
"""

import logging

from oauth2_provider.oauth2_validators import OAuth2Validator

logger = logging.getLogger(__name__)


def user_can_access(user, application) -> bool:
    """Return True iff user is active AND holds an active AppGrant for app.

    Pure function — no side effects, suitable for direct unit testing.
    """
    if not getattr(user, "is_active", False):
        return False

    # Local import to avoid the circular load path:
    # sso.permissions <- OAUTH2_PROVIDER settings <- DOT <- sso.models
    from .models import AppGrant

    return AppGrant.objects.filter(
        user=user,
        application=application,
        revoked_at__isnull=True,
    ).exists()


class SsoOAuth2Validator(OAuth2Validator):
    """OAuth2Validator override that consults AppGrant + enforces PKCE.

    DOT calls `validate_user` during password-grant; we override it
    so even an accidentally-enabled password grant cannot bypass
    AppGrant. For authorization-code grant (the only one we
    advertise), the gate lives in the project's authorize-view
    wrapper, layered on top of DOT's AuthorizationView (T13–T15).

    ``is_pkce_required`` is overridden as defense-in-depth: returns
    True for every client, so a config typo that flips
    ``OAUTH2_PROVIDER["PKCE_REQUIRED"]`` off cannot disable PKCE.
    """

    def validate_user(self, username, password, client, request, *args, **kwargs):
        # Default auth (username/password) first.
        ok = super().validate_user(username, password, client, request, *args, **kwargs)
        if not ok:
            return False

        from django.contrib.auth import get_user_model

        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            return False

        # `client` here is an oauthlib Client wrapper; the DOT
        # Application is on `.application` in some paths and is the
        # object itself in others. Handle both.
        application = getattr(client, "application", client)
        allowed = user_can_access(user, application)
        if not allowed:
            logger.info(
                "AppGrant gate denied user=%s app=%s",
                user.username,
                getattr(application, "client_id", "<unknown>"),
            )
        return allowed

    def is_pkce_required(self, client_id, request):
        # Defense-in-depth: PKCE is mandatory for every client
        # regardless of OAUTH2_PROVIDER settings.
        return True
