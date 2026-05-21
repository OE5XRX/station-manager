"""Development settings."""

import os
from pathlib import Path

os.environ.setdefault("DJANGO_SECRET_KEY", "insecure-dev-key-do-not-use-in-production")

from .base import *  # noqa: E402, F401, F403
from .base import OAUTH2_PROVIDER, OIDC_RSA_KEY_PATH  # noqa: E402

DEBUG = True

ALLOWED_HOSTS = ["*"]

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

# Disable CSP in dev for easier debugging
SECURE_CSP = None

# django-debug-toolbar
INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405
INTERNAL_IPS = ["127.0.0.1"]

# Simpler password validation in dev
AUTH_PASSWORD_VALIDATORS = []

# Email to console
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# OIDC ID-token signing key (dev only).
#
# Production reads from disk and fails loudly if missing. Dev would
# rather "just work" without forcing the developer to run
# setup_oidc_keys before they can hit /sso/ endpoints — so if the
# real key is missing, we generate an ephemeral one in process
# memory and log a loud warning so it's obvious from the boot log
# that tokens won't survive a restart.
_oidc_key_path = Path(OIDC_RSA_KEY_PATH)
if _oidc_key_path.exists():
    OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _oidc_key_path.read_text()
else:
    import logging

    from cryptography.hazmat.primitives import serialization as _serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

    _dev_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _dev_key.private_bytes(
        encoding=_serialization.Encoding.PEM,
        format=_serialization.PrivateFormat.PKCS8,
        encryption_algorithm=_serialization.NoEncryption(),
    ).decode()
    logging.getLogger("apps.sso").warning(
        "OIDC_RSA_KEY_PATH=%s missing — using an ephemeral in-memory key. "
        "Every container restart invalidates all signed tokens. Run "
        "`python manage.py setup_oidc_keys` to persist.",
        _oidc_key_path,
    )
