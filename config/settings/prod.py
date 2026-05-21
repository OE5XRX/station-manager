"""Production settings."""

import os
from pathlib import Path

from .base import *  # noqa: E402, F401, F403
from .base import OAUTH2_PROVIDER, OIDC_RSA_KEY_PATH

DEBUG = False

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")

# Security
SECURE_SSL_REDIRECT = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Health check endpoint must work over plain HTTP (Docker internal health checks
# and nginx-to-Django communication don't use HTTPS).
SECURE_REDIRECT_EXEMPT = [r"^api/v1/health/"]

# CSP: disabled for now (Django 6.0 expects a dict, not a boolean).
# TODO: configure proper CSP directives when ready to enforce.
SECURE_CSP = None

# Channel layer: Redis in production so multiple ASGI workers can share
# WebSocket groups (live status, terminal, deployment progress).
REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [(REDIS_HOST, REDIS_PORT)],
        },
    },
}

# OIDC ID-token signing key.
#
# Read lazily-ish: if the file is missing we log a CRITICAL warning
# and leave OIDC_RSA_PRIVATE_KEY unset. Django itself can still boot
# (so `collectstatic` during image build, `migrate` at deploy, etc.
# all work). DOT will then fail with a clear error on the FIRST
# request to any OIDC endpoint — exactly when an operator can see
# the problem.
#
# We intentionally do NOT auto-bootstrap an ephemeral key here the
# way dev.py does — in prod, a missing key is an operational error
# that must be fixed, not papered over.
_oidc_key_path = Path(OIDC_RSA_KEY_PATH)
try:
    OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _oidc_key_path.read_text()
except OSError as exc:
    # Catch the full OSError hierarchy (FileNotFoundError, PermissionError,
    # IsADirectoryError, broken-mount EIO, ...) so any file-read failure
    # gets the same boot-tolerant treatment: log CRITICAL, leave
    # OIDC_RSA_PRIVATE_KEY unset, let Django still boot (collectstatic /
    # migrate / check work) and DOT fail loudly on the first OIDC request.
    # UnicodeDecodeError is also possible if the file is corrupted; include
    # it too.
    import logging

    logging.getLogger("apps.sso").critical(
        "OIDC_RSA_KEY_PATH=%s unreadable (%s: %s). OIDC endpoints will "
        "return 500 on the next request. Run `python manage.py "
        "setup_oidc_keys` on the host to fix.",
        _oidc_key_path,
        type(exc).__name__,
        exc,
    )
except UnicodeDecodeError as exc:
    import logging

    logging.getLogger("apps.sso").critical(
        "OIDC_RSA_KEY_PATH=%s contains non-UTF-8 data (%s); key file is "
        "corrupted. Regenerate with `python manage.py setup_oidc_keys "
        "--force`.",
        _oidc_key_path,
        exc,
    )
