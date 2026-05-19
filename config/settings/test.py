"""Test settings — uses SQLite for fast local testing without PostgreSQL."""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-secret-key-not-for-production")

from .base import *  # noqa: E402, F401, F403

DEBUG = True

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

AUTH_PASSWORD_VALIDATORS = []

SECURE_CSP = None

# Use simple static file storage for tests (no manifest)
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.InMemoryStorage",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

# Faster password hashing for tests
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

# In-memory RSA key for tests — no filesystem dependency, so the
# suite runs in any environment without setup_oidc_keys having been
# called first.
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_test_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _test_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
