"""OIDC access-control validator.

Subclasses DOT's default OAuth2Validator. Real AppGrant + is_active
gating arrives in Task 9; for now this is a pass-through so the
discovery endpoint and any other OIDC machinery can boot.
"""

from oauth2_provider.oauth2_validators import OAuth2Validator


class SsoOAuth2Validator(OAuth2Validator):
    pass
