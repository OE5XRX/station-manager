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

# Map our custom claim names to the OIDC scopes that gate them.
# DOT filters claims through ``oidc_claim_scope``: a claim only ends
# up in the ID token if the request includes the gating scope. The
# default mapping covers the IANA-registered claims (sub, name,
# email, ...) but not our custom ``groups`` claim, so we extend the
# mapping here. ``preferred_username``/``email``/``name`` are already
# present in DOT's default map and don't need to be repeated.
SSO_CLAIM_SCOPE = dict(OAuth2Validator.oidc_claim_scope or {})
SSO_CLAIM_SCOPE["groups"] = "groups"


def user_can_access(user, application) -> bool:
    """Return True iff user is active AND policy/grant allows access.

    Spec §3.2: rote Linie ist inactive=False; alle 5 Policies haben das
    als Grundvoraussetzung. Wenn keine ApplicationPolicy-Row existiert,
    faellt der Code auf das pre-existierende GRANT_REQUIRED-Verhalten
    zurueck (abwaertskompatibel).
    """
    if not getattr(user, "is_active", False):
        return False

    # Local imports keep the validator/permissions module free of an
    # import cycle on settings loading (see existing pattern below).
    from .models import AppGrant, ApplicationPolicy

    Policy = ApplicationPolicy.AccessPolicy  # noqa: N806 — enum class alias
    policy = Policy.GRANT_REQUIRED
    pol_obj = getattr(application, "sso_policy", None)
    if pol_obj is not None:
        policy = pol_obj.access_policy

    if policy == Policy.OPEN_TO_ALL:
        return True
    if policy == Policy.OPEN_TO_MEMBERS:
        return user.membership_level != user.MembershipLevel.APPLICANT
    if policy == Policy.OPEN_TO_INTERNAL:
        return user.is_internal
    if policy == Policy.OPEN_TO_ADMINS:
        return user.is_admin

    # GRANT_REQUIRED — pre-existing behaviour
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

    # Extend DOT's claim<->scope map so the custom ``groups`` claim
    # actually makes it through ``get_oidc_claims`` and into the ID
    # token when the RP requests ``scope=... groups``.
    oidc_claim_scope = SSO_CLAIM_SCOPE

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

    def is_pkce_required(self, client_id, request=None):
        # Defense-in-depth: PKCE is mandatory for every client
        # regardless of OAUTH2_PROVIDER settings.
        #
        # `request=None` keeps the signature tolerant across DOT 3.x:
        # older 3.x releases called this with one positional arg
        # (just client_id); 3.2+ passes both. Accepting the kwarg with
        # a default works for both call shapes without forcing a
        # tighter requirements pin than what we've tested against.
        return True

    def get_additional_claims(self, request):
        """Inject our custom OIDC claims into ID tokens.

        ``OIDC_USERINFO_HOOK`` only feeds the /userinfo/ endpoint;
        ID-token claims have to be plumbed through this validator
        hook instead. Both paths reuse ``apps.sso.oidc_claims.add_claims``
        so RPs see identical data regardless of which endpoint they
        prefer (InvenTree reads the ID token, Grafana hits userinfo).
        """
        from .oidc_claims import add_claims

        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return {}
        return add_claims({}, user, request)

    def save_bearer_token(self, token, request, *args, **kwargs):
        """Override DOT hook to record a TokenSession after token-issue.

        Spec §4.2. Session tracking is observability, NOT a security gate
        -- a DB error here must NEVER block token issuance. All work is
        wrapped in try/except with logger.exception.
        """
        super().save_bearer_token(token, request, *args, **kwargs)
        try:
            self._record_token_session(token, request)
        except Exception:
            logger.exception("TokenSession recording failed")

    def _record_token_session(self, token, request):
        from django.db import transaction
        from django.utils import timezone
        from oauth2_provider.models import RefreshToken

        from .geoip import lookup_location
        from .models import SsoAuditLog, TokenSession

        refresh_value = token.get("refresh_token") if isinstance(token, dict) else None
        if not refresh_value:
            return  # No refresh -> no session row (e.g. client_credentials)
        rt = RefreshToken.objects.filter(token=refresh_value).first()
        if rt is None:
            return

        # Wrap parent.save + TokenSession.create + audit.log in a single
        # transaction so a DB error between writes cannot leave audit /
        # session state half-applied. The outer save_bearer_token catches
        # the rollback exception and logs it; DOT's own token writes have
        # already committed before this hook runs, so production token
        # issuance is unaffected.
        with transaction.atomic():
            # Refresh-rotation detection: oauthlib's DOT validator attaches
            # the previous RefreshToken instance to the request as
            # ``request.refresh_token_instance`` in ``validate_refresh_token``
            # (verified against the installed DOT source). For initial
            # issuance the attribute is absent / None.
            #
            # Note: DOT's _save_bearer_token clears the attribute after a
            # successful revoke on rotation (sets it to None). As a fallback
            # for that case we walk the new AccessToken.source_refresh_token
            # FK, which DOT wires up to the previous RefreshToken when a new
            # token pair is minted.
            parent_session = None
            old_refresh = getattr(request, "refresh_token_instance", None)
            if old_refresh is None:
                new_at = getattr(rt, "access_token", None)
                if new_at is not None:
                    old_refresh = getattr(new_at, "source_refresh_token", None)
            if old_refresh is not None:
                parent_session = TokenSession.objects.filter(
                    refresh_token=old_refresh,
                ).first()
                if parent_session is not None:
                    now = timezone.now()
                    parent_session.last_seen_at = now
                    parent_session.revoked_at = now
                    parent_session.revoke_reason = TokenSession.RevokeReason.ROTATED
                    parent_session.save(
                        update_fields=[
                            "last_seen_at",
                            "revoked_at",
                            "revoke_reason",
                        ]
                    )

            ip = self._extract_ip(request)
            ua = ""
            headers = getattr(request, "headers", None) or {}
            ua = (headers.get("HTTP_USER_AGENT") or headers.get("User-Agent") or "")[:512]
            country, city = lookup_location(ip)

            TokenSession.objects.create(
                user=rt.user,
                application=rt.application,
                refresh_token=rt,
                parent=parent_session,
                ip_address=ip,
                user_agent=ua,
                country_code=country or "",
                city=city or "",
            )

            # LOGIN_SUCCESS audit only on initial issuance, not on every
            # refresh-rotation (would be noisy and not actionable).
            if parent_session is None:
                SsoAuditLog.log(
                    event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
                    target_user=rt.user,
                    application=rt.application,
                    message=f"Token issued. UA={ua[:80]} City={city or 'unknown'}",
                    ip_address=ip,
                )

    @staticmethod
    def _extract_ip(request):
        """Return the client IP from oauthlib request headers.

        oauthlib's extract_headers copies Django's request.META keys
        verbatim, so the production-reality keys are
        HTTP_X_FORWARDED_FOR / HTTP_X_REAL_IP. Unit tests using
        SimpleNamespace with the HTTP standard names (X-Forwarded-For,
        X-Real-IP) are also supported via the second-arg fallback.
        """
        headers = getattr(request, "headers", None) or {}
        xff = headers.get("HTTP_X_FORWARDED_FOR") or headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return headers.get("HTTP_X_REAL_IP") or headers.get("X-Real-IP")
