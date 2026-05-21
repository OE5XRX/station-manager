"""Custom OIDC claims emitted in ID tokens and UserInfo responses.

In DOT, ID-token claims and UserInfo claims are populated through
**two separate hooks**:

- ID token (token endpoint): ``SsoOAuth2Validator.get_additional_claims``
  in ``apps.sso.permissions`` calls ``add_claims`` to inject the
  extra fields.
- UserInfo endpoint (``/sso/userinfo/``): ``OAUTH2_PROVIDER[
  "OIDC_USERINFO_HOOK"]`` is wired directly to ``add_claims``.

Both paths funnel through this single function so RPs see identical
data regardless of which endpoint they prefer (InvenTree reads the
ID token, Grafana hits userinfo). Convention: every claim added
here is documented in the RP integration guide
(docs/superpowers/specs/2026-05-18-sso-oidc-provider-design.md,
Section 3.4) so InvenTree / Grafana operators know what to expect.
"""


def add_claims(claims, user, request):
    """Merge OE5XRX-specific claims into the OIDC payload.

    `claims` is a dict the caller hands in; mutate-or-return is fine
    (we do both to be safe across DOT versions: return value is what
    DOT actually uses).
    """
    claims["preferred_username"] = user.username
    claims["email"] = user.email or ""
    claims["email_verified"] = bool(user.email)
    claims["name"] = user.get_full_name() or user.username
    claims["locale"] = getattr(user, "language", "en") or "en"
    claims["groups"] = list(user.groups.values_list("name", flat=True))
    return claims
