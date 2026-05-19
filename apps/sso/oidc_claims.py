"""OIDC claims hook.

Wired into OAUTH2_PROVIDER["OIDC_USERINFO_HOOK"]. Real custom claims
land in Task 8; for now this is a pass-through.
"""


def add_claims(claims, user, request):
    return claims
