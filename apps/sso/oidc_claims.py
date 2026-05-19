"""Custom OIDC claims emitted in ID tokens and UserInfo responses.

Wired in via OAUTH2_PROVIDER["OIDC_USERINFO_HOOK"]. Called by DOT
when building the ID token (token endpoint) and the userinfo
endpoint response.

Convention: every claim added here is documented in the RP
integration guide
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
