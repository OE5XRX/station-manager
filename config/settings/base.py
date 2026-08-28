"""
Base settings for OE5XRX Station Manager.
"""

import os
from pathlib import Path

from django.utils.csp import CSP
from django.utils.translation import gettext_lazy as _

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("DJANGO_SECRET_KEY environment variable must be set.")

DEBUG = False

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(",")

# Application definition

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "oauth2_provider",
    "rest_framework",
    "django_htmx",
    "storages",
    "axes",
    # Local apps
    "apps.accounts",
    "apps.api",
    "apps.dashboard",
    "apps.stations",
    "apps.deployments",
    "apps.tunnel",
    "apps.control",
    "apps.audit",
    "apps.monitoring",
    "apps.webpush",
    "apps.images",
    "apps.provisioning",
    "apps.rollouts",
    "apps.sso",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Default-deny: every view is login-required unless decorated with
    # ``django.contrib.auth.decorators.login_not_required`` or covered
    # by our subclass's exact-path / true-prefix allow-list (OIDC
    # public surface + i18n setlang). Must come AFTER
    # AuthenticationMiddleware so that ``request.user`` is populated.
    # See ``config/middleware.py`` and issue #73 for the rationale.
    "config.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "axes.middleware.AxesMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "station_manager"),
        "USER": os.environ.get("POSTGRES_USER", "station_manager"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "station_manager"),
        "HOST": os.environ.get("POSTGRES_HOST", "db"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}

# Auth
AUTH_USER_MODEL = "accounts.User"

# Sub-Spec 2b Soft-Delete: accounts.User.username is intentionally
# unique=False at the field level. DB-level uniqueness is enforced via a
# conditional UniqueConstraint (unique_active_username) that only applies
# to non-soft-deleted rows. Django's auth.W004 system check does not see
# the conditional constraint and warns about USERNAME_FIELD not being
# unique — the warning is a false positive in this setup.
SILENCED_SYSTEM_CHECKS = ["auth.W004"]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "dashboard:index"
LOGOUT_REDIRECT_URL = "accounts:login"

# Internationalization
LANGUAGE_CODE = os.environ.get("DJANGO_LANGUAGE_CODE", "en")
LANGUAGES = [
    ("en", _("English")),
    ("de", _("German")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True

TIME_ZONE = "Europe/Vienna"
USE_TZ = True

# Static files
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Media files (station photos)
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# S3-compatible storage (Hetzner Object Storage, AWS S3, MinIO, etc.)
# Set USE_S3=true + S3 env vars to enable, otherwise local filesystem is used
if os.environ.get("USE_S3", "false").lower() == "true":
    STORAGES["default"] = {
        "BACKEND": "storages.backends.s3boto3.S3Boto3Storage",
    }
    AWS_ACCESS_KEY_ID = os.environ["S3_ACCESS_KEY"]
    AWS_SECRET_ACCESS_KEY = os.environ["S3_SECRET_KEY"]
    AWS_STORAGE_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
    AWS_S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")
    AWS_S3_REGION_NAME = os.environ.get("S3_REGION", "")
    AWS_DEFAULT_ACL = None
    AWS_S3_FILE_OVERWRITE = False

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django REST Framework
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.api.authentication.DeviceKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.ScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "heartbeat": "10/min",
        "register": "10/hour",
    },
}

# Django Channels
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}

# Control-plane (D4) tunables.
CONTROL_T_IDLE_SECONDS = 300  # idle lock auto-free (5 min)
CONTROL_RECONNECT_GRACE_SECONDS = 12  # hold survives a short WS blip
CONTROL_COMMAND_TIMEOUT_SECONDS = 10  # no result -> timeout error to browser
CONTROL_LOCK_SWEEP_INTERVAL_SECONDS = 5

# Django Tasks Framework
TASKS = {
    "default": {
        "BACKEND": "django.tasks.backends.immediate.ImmediateBackend",
    },
}

# Content Security Policy
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF, CSP.NONCE],
    "style-src": [CSP.SELF, CSP.NONCE, "https://cdn.jsdelivr.net", "https://fonts.googleapis.com"],
    "font-src": [CSP.SELF, "https://cdn.jsdelivr.net", "https://fonts.gstatic.com"],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF, "ws:", "wss:"],
}

# Session security
#
# 7-day rolling session: COOKIE_AGE is the absolute max-age sent on each
# Set-Cookie, and SAVE_EVERY_REQUEST=True makes Django re-send the cookie
# on every response — even when the view did not touch request.session.
# Together: as long as the operator hits the UI at least once per week,
# their session never expires. After 7 days of true inactivity it's gone.
#
# Tradeoff: SAVE_EVERY_REQUEST writes the session row on every
# Django-handled request. Static assets short-circuit through
# WhiteNoiseMiddleware before SessionMiddleware runs (see MIDDLEWARE
# order above) so they cost nothing here. For an internal fleet tool
# with a handful of operators the per-request write is negligible;
# re-evaluate if user count grows by 2+ orders of magnitude.
SESSION_COOKIE_AGE = 7 * 24 * 60 * 60  # 7 days
SESSION_SAVE_EVERY_REQUEST = True
SESSION_COOKIE_SAMESITE = "Lax"

# django-axes (brute force protection)
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1  # hours
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]

# Alert notifications — Email
ALERT_EMAIL_ENABLED = os.environ.get("ALERT_EMAIL_ENABLED", "false").lower() == "true"

# Public base URL for stations to reach this server. Baked into the
# station-agent's config.yml at provisioning time — see
# apps/provisioning/management/commands/run_background_jobs.py and
# apps/provisioning/config_render.py. Empty = provisioning fails loud
# rather than poisoning new images with a stale URL.
SERVER_PUBLIC_URL = os.environ.get("SERVER_PUBLIC_URL", "")

EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() == "true"
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.environ.get("EMAIL_FROM", "alerts@oe5xrx.org")
SITE_URL = os.environ.get("SITE_URL", "https://remote.oe5xrx.org")

# Alert notifications — Telegram
ALERT_TELEGRAM_ENABLED = os.environ.get("ALERT_TELEGRAM_ENABLED", "false").lower() == "true"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Web-Push (PWA) notifications — third alert channel.
# VAPID keys are generated via `manage.py generate_vapid_keys` and injected
# via env/secrets, never committed. The channel stays silently disabled
# until both keys are present (mirrors ALERT_EMAIL_ENABLED semantics).
WEBPUSH_VAPID_PUBLIC_KEY = os.environ.get("WEBPUSH_VAPID_PUBLIC_KEY", "")
WEBPUSH_VAPID_PRIVATE_KEY = os.environ.get("WEBPUSH_VAPID_PRIVATE_KEY", "")
WEBPUSH_VAPID_ADMIN_EMAIL = os.environ.get("WEBPUSH_VAPID_ADMIN_EMAIL", "")
ALERT_WEBPUSH_ENABLED = bool(WEBPUSH_VAPID_PUBLIC_KEY and WEBPUSH_VAPID_PRIVATE_KEY)

# Django OAuth Toolkit — OIDC provider configuration.
# Issuer must match the public URL prefix (see /sso/ in config/urls.py).
# RSA private key path is resolved at runtime by the setup_oidc_keys
# management command (Task 4); the file lives on a persistent volume
# so token signatures survive container restarts.
#
# OIDC_ISS_ENDPOINT MUST be set explicitly in prod.py (or via env) to
# the public base URL — e.g. "https://ham.oe5xrx.org/sso". Without it
# DOT auto-derives the issuer from the request host, which drifts
# behind nginx (SECURE_PROXY_SSL_HEADER masks the original scheme) and
# breaks token validation on every RP. Empty default = DOT auto-derive,
# which is fine for dev/test but not prod.
OIDC_RSA_KEY_PATH = os.environ.get(
    "OIDC_RSA_KEY_PATH",
    str(BASE_DIR / "oidc_keys" / "private.pem"),
)

# db-ip.com City Lite database path. Refreshed daily by the
# update-geoip-db GitHub-Actions cron (see servers repo). If the file
# is missing, lookups silently return (None, None) — session rows get
# empty country/city, token issuance is never blocked.
GEOIP_DB_PATH = os.environ.get(
    "GEOIP_DB_PATH",
    str(BASE_DIR / "geoip_db" / "dbip-city-lite.mmdb"),
)

OAUTH2_PROVIDER = {
    "OIDC_ENABLED": True,
    "OIDC_ISS_ENDPOINT": os.environ.get("OIDC_ISS_ENDPOINT", ""),
    # OIDC_RSA_PRIVATE_KEY is read lazily in prod.py / dev.py overrides
    # (it must exist at startup) — base.py only declares the path.
    "SCOPES": {
        "openid": "OpenID Connect",
        "profile": "User profile",
        "email": "Email address",
        "groups": "Group memberships",
    },
    "DEFAULT_SCOPES": ["openid"],
    "PKCE_REQUIRED": True,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,  # 1 h
    "ID_TOKEN_EXPIRE_SECONDS": 3600,  # 1 h
    "REFRESH_TOKEN_EXPIRE_SECONDS": 14 * 24 * 3600,  # 14 d
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,  # 60 s
    "ROTATE_REFRESH_TOKEN": True,
    "OAUTH2_VALIDATOR_CLASS": "apps.sso.permissions.SsoOAuth2Validator",
    "OIDC_USERINFO_HOOK": "apps.sso.oidc_claims.add_claims",
}

# Swappable-model bindings for django-oauth-toolkit. These are the
# DOT defaults but MUST be defined at the top level (not inside
# OAUTH2_PROVIDER) so Django's migration autodetector can resolve
# string-form FKs like ForeignKey("oauth2_provider.Application", …)
# in our own apps (e.g. AppGrant in apps.sso). Without them,
# makemigrations crashes with AttributeError on the swappable lookup.
OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth2_provider.Application"
OAUTH2_PROVIDER_ACCESS_TOKEN_MODEL = "oauth2_provider.AccessToken"
OAUTH2_PROVIDER_ID_TOKEN_MODEL = "oauth2_provider.IDToken"
OAUTH2_PROVIDER_GRANT_MODEL = "oauth2_provider.Grant"
OAUTH2_PROVIDER_REFRESH_TOKEN_MODEL = "oauth2_provider.RefreshToken"
