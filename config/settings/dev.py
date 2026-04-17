"""Development settings."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "insecure-dev-key-do-not-use-in-production")

from .base import *  # noqa: E402, F401, F403

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
