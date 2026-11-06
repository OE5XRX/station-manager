"""Test settings on PostgreSQL — only for the history tests (partitioned tables).

CI job ``test-history-postgres`` runs ``tests/test_history_*.py`` with these settings; the
rest of the suite stays on SQLite (``config.settings.test``).
"""

import os

from .test import *  # noqa: F401, F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "station_manager"),
        "USER": os.environ.get("POSTGRES_USER", "station_manager"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "station_manager"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}
