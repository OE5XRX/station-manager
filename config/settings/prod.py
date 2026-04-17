"""Production settings."""

import os

from .base import *  # noqa: F401, F403

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
