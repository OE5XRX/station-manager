# PWA + Web-Push Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** station-manager als installierbare iPhone-PWA mit Web-Push als drittem Alert-Kanal neben E-Mail und Telegram, gesteuert über eine Pro-User-Präferenz mit E-Mail-Fallback.

**Architecture:** Neue App `apps/webpush` kapselt Push-Subscriptions, VAPID-Handling und den `pywebpush`-Dispatch. `apps/monitoring` konsumiert die Dispatch-Funktion als dritten Kanal (analog zu `send_mail` heute); das topologie-basierte Empfänger-Routing wird nach `notify_channel` (neues Feld am `User`) in ein E-Mail- und ein Push-Set aufgeteilt. Service Worker und Web-App-Manifest werden über dünne Django-Views mit stabilen URLs ausgeliefert (WhiteNoise-`CompressedManifestStaticFilesStorage` hasht sonst die Dateinamen).

**Tech Stack:** Django 6.0, Python 3.14, `pywebpush`, `cryptography` (schon vorhanden), WhiteNoise, Bootstrap 5 + HTMX, pytest + pytest-django.

**Spec:** `docs/superpowers/specs/2026-08-28-pwa-web-push-design.md`

## Global Constraints

- Python-Deps über `requirements/base.in` pflegen, dann `uv pip compile` → `base.txt` (nicht `base.txt` von Hand editieren).
- Version-Regel: neueste stabile Version pinnen mit Minor-Cap (`>=X.Y,<X.(Y+1)`-Stil wie im bestehenden `base.in`).
- Django-Templates: **niemals** multi-line `{# … #}`; nur `{% comment %} … {% endcomment %}`. CI-Template-Guard ist aktiv.
- VAPID-Private-Key nur aus Env/Secrets, nie in DB oder Repo (analog OIDC-Keys).
- Tests liegen in top-level `tests/`, laufen mit `DJANGO_SETTINGS_MODULE=config.settings.test` via `pytest`.
- Default `notify_channel = EMAIL` → Bestandsverhalten darf sich für niemanden ändern, solange nicht aktiv Push aktiviert wird.
- CSP nutzt Nonce (`{{ csp_nonce }}`); jedes Inline-`<script>` braucht `nonce="{{ csp_nonce }}"`. `default-src`/`script-src`/`connect-src` sind bereits `self` — **keine** CSP-Änderung nötig (Manifest, `/sw.js`, Subscribe-POST sind alle same-origin).

---

### Task 1: Dependencies + Settings-Flags

**Files:**
- Modify: `requirements/base.in`
- Modify: `requirements/base.txt` (generiert)
- Modify: `config/settings/base.py` (nach den `TELEGRAM_*`-Zeilen, ~274)
- Test: `tests/test_webpush_settings.py`

**Interfaces:**
- Produces: `settings.WEBPUSH_VAPID_PUBLIC_KEY: str`, `settings.WEBPUSH_VAPID_PRIVATE_KEY: str`, `settings.WEBPUSH_VAPID_ADMIN_EMAIL: str`, `settings.ALERT_WEBPUSH_ENABLED: bool`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webpush_settings.py
"""Pins the webpush feature-flag semantics: disabled unless VAPID keys set."""
import importlib

from django.conf import settings


def test_webpush_disabled_without_keys(monkeypatch):
    # Default test env sets no VAPID keys → channel is off.
    assert settings.ALERT_WEBPUSH_ENABLED is False


def test_webpush_settings_names_exist():
    assert hasattr(settings, "WEBPUSH_VAPID_PUBLIC_KEY")
    assert hasattr(settings, "WEBPUSH_VAPID_PRIVATE_KEY")
    assert hasattr(settings, "WEBPUSH_VAPID_ADMIN_EMAIL")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webpush_settings.py -v`
Expected: FAIL (`AttributeError` / `ALERT_WEBPUSH_ENABLED` missing).

- [ ] **Step 3: Add dependency**

In `requirements/base.in`, after the `python-telegram-bot` line, add:

```
pywebpush>=2.0,<3.0
```

Then regenerate the lockfile:

```bash
uv pip compile requirements/base.in -o requirements/base.txt
uv pip install -r requirements/base.txt
```

(Check the current newest stable of `pywebpush` on PyPI first; adjust the cap to `<next-major`.)

- [ ] **Step 4: Add settings**

In `config/settings/base.py`, after the `TELEGRAM_CHAT_ID` line (~274):

```python
# Web-Push (PWA) notifications — third alert channel.
# VAPID keys are generated via `manage.py generate_vapid_keys` and injected
# via env/secrets, never committed. The channel stays silently disabled
# until both keys are present (mirrors ALERT_EMAIL_ENABLED semantics).
WEBPUSH_VAPID_PUBLIC_KEY = os.environ.get("WEBPUSH_VAPID_PUBLIC_KEY", "")
WEBPUSH_VAPID_PRIVATE_KEY = os.environ.get("WEBPUSH_VAPID_PRIVATE_KEY", "")
WEBPUSH_VAPID_ADMIN_EMAIL = os.environ.get("WEBPUSH_VAPID_ADMIN_EMAIL", "")
ALERT_WEBPUSH_ENABLED = bool(WEBPUSH_VAPID_PUBLIC_KEY and WEBPUSH_VAPID_PRIVATE_KEY)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_webpush_settings.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add requirements/base.in requirements/base.txt config/settings/base.py tests/test_webpush_settings.py
git commit -m "feat(webpush): pywebpush dependency + VAPID settings flags"
```

---

### Task 2: `apps/webpush` app + `PushSubscription` model

**Files:**
- Create: `apps/webpush/__init__.py`
- Create: `apps/webpush/apps.py`
- Create: `apps/webpush/models.py`
- Create: `apps/webpush/migrations/__init__.py`
- Create: `apps/webpush/admin.py`
- Modify: `config/settings/base.py` (INSTALLED_APPS, after `"apps.monitoring",` ~48)
- Test: `tests/test_webpush_model.py`

**Interfaces:**
- Produces: `apps.webpush.models.PushSubscription` with fields `user` (FK, `related_name="push_subscriptions"`), `endpoint` (unique), `p256dh`, `auth`, `label`, `created_at`, `last_success_at`, `failure_count`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webpush_model.py
import pytest
from django.db import IntegrityError

from apps.accounts.models import User
from apps.webpush.models import PushSubscription


def _sub(user, endpoint="https://push.example/abc"):
    return PushSubscription.objects.create(
        user=user, endpoint=endpoint, p256dh="p", auth="a"
    )


@pytest.mark.django_db
def test_subscription_created_with_defaults():
    u = User.objects.create_user(username="a", password="x", email="a@x")
    s = _sub(u)
    assert s.failure_count == 0
    assert s.last_success_at is None
    assert list(u.push_subscriptions.all()) == [s]


@pytest.mark.django_db
def test_endpoint_is_unique():
    u = User.objects.create_user(username="b", password="x", email="b@x")
    _sub(u)
    with pytest.raises(IntegrityError):
        _sub(u)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webpush_model.py -v`
Expected: FAIL (`ModuleNotFoundError: apps.webpush`).

- [ ] **Step 3: Create the app**

`apps/webpush/__init__.py`: empty file.

`apps/webpush/apps.py`:

```python
from django.apps import AppConfig


class WebpushConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.webpush"
```

`apps/webpush/migrations/__init__.py`: empty file.

`apps/webpush/models.py`:

```python
"""Web-Push subscription storage.

One row per browser push endpoint. The endpoint URL (issued by the
platform push service — Apple/Mozilla/Google) is the natural unique key:
re-subscribing the same browser yields the same endpoint, so we upsert
rather than duplicate. VAPID handling and delivery live in
``apps/webpush/dispatch.py``; this module is storage only.
"""

from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="push_subscriptions",
    )
    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=255)
    auth = models.CharField(max_length=255)
    label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    failure_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} · {self.label or self.endpoint[:40]}"

    @property
    def subscription_info(self):
        """Return the dict shape pywebpush expects."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }
```

`apps/webpush/admin.py`:

```python
from django.contrib import admin

from .models import PushSubscription


@admin.register(PushSubscription)
class PushSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "label", "endpoint", "last_success_at", "failure_count")
    search_fields = ("user__username", "endpoint", "label")
    readonly_fields = ("created_at", "last_success_at")
```

- [ ] **Step 4: Register the app + migrate**

In `config/settings/base.py` INSTALLED_APPS, after `"apps.monitoring",`:

```python
    "apps.webpush",
```

Then:

```bash
python manage.py makemigrations webpush
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_webpush_model.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/webpush config/settings/base.py tests/test_webpush_model.py
git commit -m "feat(webpush): PushSubscription model + app scaffold"
```

---

### Task 3: VAPID key generation command

**Files:**
- Create: `apps/webpush/management/__init__.py`
- Create: `apps/webpush/management/commands/__init__.py`
- Create: `apps/webpush/management/commands/generate_vapid_keys.py`
- Test: `tests/test_webpush_vapid.py`

**Interfaces:**
- Produces: management command `generate_vapid_keys` printing `WEBPUSH_VAPID_PUBLIC_KEY=<b64url>` and `WEBPUSH_VAPID_PRIVATE_KEY=<b64url>`. Private key = base64url of the raw 32-byte EC-P256 scalar (the format `pywebpush`/`py_vapid` accept as a string). Public key = base64url of the uncompressed EC point (the browser `applicationServerKey`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webpush_vapid.py
import base64

from django.core.management import call_command
from io import StringIO


def _b64url_decode(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_generate_vapid_keys_emits_valid_pair():
    out = StringIO()
    call_command("generate_vapid_keys", stdout=out)
    lines = dict(
        line.split("=", 1) for line in out.getvalue().splitlines() if "=" in line
    )
    assert "WEBPUSH_VAPID_PUBLIC_KEY" in lines
    assert "WEBPUSH_VAPID_PRIVATE_KEY" in lines
    # raw scalar is 32 bytes; uncompressed point is 65 bytes (0x04 + X + Y)
    assert len(_b64url_decode(lines["WEBPUSH_VAPID_PRIVATE_KEY"])) == 32
    pub = _b64url_decode(lines["WEBPUSH_VAPID_PUBLIC_KEY"])
    assert len(pub) == 65 and pub[0] == 0x04
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webpush_vapid.py -v`
Expected: FAIL (`Unknown command: 'generate_vapid_keys'`).

- [ ] **Step 3: Implement the command**

`apps/webpush/management/__init__.py` and `apps/webpush/management/commands/__init__.py`: empty files.

`apps/webpush/management/commands/generate_vapid_keys.py`:

```python
"""Generate a VAPID keypair for Web-Push.

Outputs env-ready lines. The private key is the base64url-encoded raw
32-byte EC-P256 private scalar (the form pywebpush accepts as a string);
the public key is the base64url-encoded uncompressed point used by the
browser as ``applicationServerKey``. Keys go into env/secrets — never DB
or repo (mirrors the OIDC-key handling).
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.management.base import BaseCommand


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class Command(BaseCommand):
    help = "Generate a VAPID keypair for Web-Push notifications."

    def handle(self, *args, **options):
        key = ec.generate_private_key(ec.SECP256R1())
        priv_raw = key.private_numbers().private_value.to_bytes(32, "big")
        pub_point = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        self.stdout.write(f"WEBPUSH_VAPID_PUBLIC_KEY={_b64url(pub_point)}")
        self.stdout.write(f"WEBPUSH_VAPID_PRIVATE_KEY={_b64url(priv_raw)}")
        self.stdout.write(
            "WEBPUSH_VAPID_ADMIN_EMAIL=mailto:admin@oe5xrx.org  # adjust"
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_webpush_vapid.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/webpush/management tests/test_webpush_vapid.py
git commit -m "feat(webpush): generate_vapid_keys management command"
```

---

### Task 4: `send_web_push()` dispatch + expiry handling

**Files:**
- Create: `apps/webpush/dispatch.py`
- Test: `tests/test_webpush_dispatch.py`

**Interfaces:**
- Consumes: `PushSubscription` (Task 2), `settings.WEBPUSH_VAPID_PRIVATE_KEY` / `WEBPUSH_VAPID_ADMIN_EMAIL` (Task 1).
- Produces: `send_web_push(subscription: PushSubscription, payload: dict) -> bool`. Success → `last_success_at` set, `failure_count` reset, returns `True`. `WebPushException` status 404/410 → subscription **deleted**, returns `False`. Other error → `failure_count += 1`, returns `False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webpush_dispatch.py
from unittest import mock

import pytest

from apps.accounts.models import User
from apps.webpush.models import PushSubscription
from apps.webpush import dispatch


def _sub(endpoint="https://push.example/x"):
    u = User.objects.create_user(username="a", password="x", email="a@x")
    return PushSubscription.objects.create(
        user=u, endpoint=endpoint, p256dh="p", auth="a"
    )


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


@pytest.mark.django_db
def test_success_updates_timestamps(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    with mock.patch.object(dispatch, "webpush") as m:
        ok = dispatch.send_web_push(s, {"title": "t", "body": "b"})
    assert ok is True
    m.assert_called_once()
    s.refresh_from_db()
    assert s.last_success_at is not None
    assert s.failure_count == 0


@pytest.mark.django_db
def test_expired_subscription_is_deleted(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    exc = dispatch.WebPushException("gone", response=_Resp(410))
    with mock.patch.object(dispatch, "webpush", side_effect=exc):
        ok = dispatch.send_web_push(s, {"title": "t"})
    assert ok is False
    assert not PushSubscription.objects.filter(pk=s.pk).exists()


@pytest.mark.django_db
def test_transient_error_increments_failure(settings):
    settings.WEBPUSH_VAPID_PRIVATE_KEY = "priv"
    settings.WEBPUSH_VAPID_ADMIN_EMAIL = "mailto:a@x"
    s = _sub()
    exc = dispatch.WebPushException("boom", response=_Resp(500))
    with mock.patch.object(dispatch, "webpush", side_effect=exc):
        ok = dispatch.send_web_push(s, {"title": "t"})
    assert ok is False
    s.refresh_from_db()
    assert s.failure_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webpush_dispatch.py -v`
Expected: FAIL (`ModuleNotFoundError: apps.webpush.dispatch`).

- [ ] **Step 3: Implement dispatch**

`apps/webpush/dispatch.py`:

```python
"""Deliver a single Web-Push message via pywebpush.

Kept import-light and side-effect-scoped so the monitoring dispatch can
iterate subscriptions and isolate per-device failures. Expired endpoints
(404/410) are pruned on the spot; transient errors bump a failure counter.
"""

import json
import logging

from django.conf import settings
from django.utils import timezone
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)


def send_web_push(subscription, payload):
    """Send ``payload`` (a JSON-serialisable dict) to one subscription.

    Returns True on success. On 404/410 the subscription is deleted and
    False returned. On any other error failure_count is incremented and
    False returned.
    """
    try:
        webpush(
            subscription_info=subscription.subscription_info,
            data=json.dumps(payload),
            vapid_private_key=settings.WEBPUSH_VAPID_PRIVATE_KEY,
            vapid_claims={"sub": settings.WEBPUSH_VAPID_ADMIN_EMAIL},
        )
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            logger.info("Pruning expired push subscription %s (%s).", subscription.pk, status)
            subscription.delete()
        else:
            logger.warning("Web-push failed for subscription %s: %s", subscription.pk, exc)
            subscription.failure_count += 1
            subscription.save(update_fields=["failure_count"])
        return False
    except Exception:
        logger.exception("Unexpected web-push error for subscription %s.", subscription.pk)
        subscription.failure_count += 1
        subscription.save(update_fields=["failure_count"])
        return False

    subscription.last_success_at = timezone.now()
    subscription.failure_count = 0
    subscription.save(update_fields=["last_success_at", "failure_count"])
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_webpush_dispatch.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/webpush/dispatch.py tests/test_webpush_dispatch.py
git commit -m "feat(webpush): send_web_push dispatch with expiry pruning"
```

---

### Task 5: `notify_channel` field on User

**Files:**
- Modify: `apps/accounts/models.py` (after the `language` field, ~106)
- Create: migration under `apps/accounts/migrations/`
- Test: `tests/test_notify_channel_field.py`

**Interfaces:**
- Produces: `User.NotifyChannel` (TextChoices: `EMAIL="email"`, `PUSH="push"`, `BOTH="both"`) and `User.notify_channel` (default `EMAIL`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notify_channel_field.py
import pytest

from apps.accounts.models import User


@pytest.mark.django_db
def test_notify_channel_defaults_to_email():
    u = User.objects.create_user(username="a", password="x", email="a@x")
    assert u.notify_channel == User.NotifyChannel.EMAIL


@pytest.mark.django_db
def test_notify_channel_choices():
    assert {c[0] for c in User.NotifyChannel.choices} == {"email", "push", "both"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notify_channel_field.py -v`
Expected: FAIL (`AttributeError: NotifyChannel`).

- [ ] **Step 3: Add the field**

In `apps/accounts/models.py`, directly after the `language` field block (~106, before `class MembershipLevel`):

```python
    class NotifyChannel(models.TextChoices):
        EMAIL = "email", _("Nur E-Mail")
        PUSH = "push", _("Nur Push")
        BOTH = "both", _("E-Mail und Push")

    notify_channel = models.CharField(
        _("notification channel"),
        max_length=8,
        choices=NotifyChannel.choices,
        default=NotifyChannel.EMAIL,
        help_text=_("How station alerts reach you: e-mail, push, or both."),
    )
```

Then:

```bash
python manage.py makemigrations accounts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_notify_channel_field.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/accounts/models.py apps/accounts/migrations tests/test_notify_channel_field.py
git commit -m "feat(accounts): notify_channel preference field on User"
```

---

### Task 6: Recipient routing split (email vs push, with fallback)

**Files:**
- Modify: `apps/monitoring/recipients.py`
- Test: `tests/test_alert_channel_recipients.py`

**Interfaces:**
- Consumes: `User.notify_channel` (Task 5), `PushSubscription` (Task 2).
- Produces:
  - `email_recipients_for_station_alert(station) -> QuerySet[User]` — topology set with `notify_channel in (EMAIL, BOTH)` **OR** (`notify_channel == PUSH` **AND** no push subscription), excluding empty e-mails.
  - `push_recipients_for_station_alert(station) -> QuerySet[User]` — topology set with `notify_channel in (PUSH, BOTH)` **AND** at least one push subscription.
  - `recipients_for_station_alert(station)` stays as an alias of `email_recipients_for_station_alert` (backward-compat for existing callers/tests).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_alert_channel_recipients.py
import pytest

from apps.accounts.models import User
from apps.monitoring.recipients import (
    email_recipients_for_station_alert,
    push_recipients_for_station_alert,
)
from apps.stations.models import Station, StationAssignment
from apps.webpush.models import PushSubscription


def _admin(username, channel, email=None):
    email = email or f"{username}@x"
    u = User.objects.create_user(username=username, password="x", email=email)
    u.membership_level = User.MembershipLevel.ADMIN
    u.notify_channel = channel
    u.save(update_fields=["membership_level", "notify_channel"])
    return u


def _sub(u):
    return PushSubscription.objects.create(
        user=u, endpoint=f"https://push.example/{u.pk}", p256dh="p", auth="a"
    )


@pytest.mark.django_db
def test_email_user_only_in_email_set():
    u = _admin("a", User.NotifyChannel.EMAIL)
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(email_recipients_for_station_alert(s))
    assert u not in list(push_recipients_for_station_alert(s))


@pytest.mark.django_db
def test_push_user_with_device_only_in_push_set():
    u = _admin("b", User.NotifyChannel.PUSH)
    _sub(u)
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(push_recipients_for_station_alert(s))
    assert u not in list(email_recipients_for_station_alert(s))


@pytest.mark.django_db
def test_push_user_without_device_falls_back_to_email():
    u = _admin("c", User.NotifyChannel.PUSH)  # no subscription
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(email_recipients_for_station_alert(s))
    assert u not in list(push_recipients_for_station_alert(s))


@pytest.mark.django_db
def test_both_user_with_device_in_both_sets():
    u = _admin("d", User.NotifyChannel.BOTH)
    _sub(u)
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    assert u in list(email_recipients_for_station_alert(s))
    assert u in list(push_recipients_for_station_alert(s))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_alert_channel_recipients.py -v`
Expected: FAIL (`ImportError` — new functions don't exist).

- [ ] **Step 3: Refactor recipients.py**

Replace the body of `apps/monitoring/recipients.py` (keep the module docstring) so the topology Q is shared and channel-filtered downstream. Add below the existing imports:

```python
from django.db.models import Exists, OuterRef, Q


def _topology_q(station):
    q = Q(membership_level=User.MembershipLevel.ADMIN)
    if station.region_id is not None:
        q |= Q(
            region_assignments__region_id=station.region_id,
            region_assignments__role="manager",
        )
    q |= Q(
        station_assignments__station=station,
        station_assignments__role__in=["admin", "maintainer"],
    )
    return q


def _base_topology_recipients(station):
    """Topology-routed, active, non-applicant users — WITHOUT the email
    exclusion (push recipients may legitimately have no email)."""
    return (
        User.objects.active()
        .filter(_topology_q(station))
        .exclude(is_active=False)
        .exclude(membership_level=User.MembershipLevel.APPLICANT)
        .distinct()
    )


def email_recipients_for_station_alert(station):
    """Users who should receive the alert e-mail.

    EMAIL/BOTH always; PUSH only as fallback when they have no working
    push subscription. Empty e-mails are excluded (can't mail them).
    """
    from apps.webpush.models import PushSubscription

    has_push = Exists(PushSubscription.objects.filter(user=OuterRef("pk")))
    return (
        _base_topology_recipients(station)
        .annotate(_has_push=has_push)
        .filter(
            Q(notify_channel__in=[User.NotifyChannel.EMAIL, User.NotifyChannel.BOTH])
            | Q(notify_channel=User.NotifyChannel.PUSH, _has_push=False)
        )
        .exclude(email="")
    )


def push_recipients_for_station_alert(station):
    """Users who should receive the alert as Web-Push (PUSH/BOTH with a
    registered device)."""
    return (
        _base_topology_recipients(station)
        .filter(
            notify_channel__in=[User.NotifyChannel.PUSH, User.NotifyChannel.BOTH],
            push_subscriptions__isnull=False,
        )
        .distinct()
    )
```

Then change the existing `recipients_for_station_alert` function to delegate (preserving its name for current callers/tests):

```python
def recipients_for_station_alert(station):
    """Backward-compatible alias: the e-mail recipient set."""
    return email_recipients_for_station_alert(station)
```

(Remove the old inline body of `recipients_for_station_alert` — the logic now lives in `_base_topology_recipients` + `email_recipients_for_station_alert`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_alert_channel_recipients.py tests/test_alert_recipients.py -v`
Expected: PASS (both the new channel tests **and** the existing topology tests — default EMAIL keeps them green).

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/recipients.py tests/test_alert_channel_recipients.py
git commit -m "feat(monitoring): split alert recipients into email/push sets with fallback"
```

---

### Task 7: Wire Web-Push into `send_alert_notifications`

**Files:**
- Modify: `apps/monitoring/notifications.py`
- Test: `tests/test_notification_dispatch_webpush.py`

**Interfaces:**
- Consumes: `push_recipients_for_station_alert` (Task 6), `apps.webpush.dispatch.send_web_push` (Task 4), `settings.ALERT_WEBPUSH_ENABLED` (Task 1).
- Produces: `_send_webpush_notification(alert)`; `send_alert_notifications` gains a third `if settings.ALERT_WEBPUSH_ENABLED` branch.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notification_dispatch_webpush.py
from unittest import mock

import pytest
from django.core import mail

from apps.accounts.models import User
from apps.monitoring.models import Alert, AlertRule
from apps.monitoring.notifications import send_alert_notifications
from apps.stations.models import Station, StationAssignment
from apps.webpush.models import PushSubscription


def _admin(username, channel):
    u = User.objects.create_user(username=username, password="x", email=f"{username}@x")
    u.membership_level = User.MembershipLevel.ADMIN
    u.notify_channel = channel
    u.save(update_fields=["membership_level", "notify_channel"])
    return u


def _alert(station):
    rule = AlertRule.objects.get(alert_type=AlertRule.AlertType.STATION_OFFLINE)
    return Alert.objects.create(
        station=station, alert_rule=rule, severity="critical", title="T", message="m"
    )


@pytest.mark.django_db
def test_both_channel_triggers_email_and_push(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.ALERT_WEBPUSH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    u = _admin("a", User.NotifyChannel.BOTH)
    PushSubscription.objects.create(
        user=u, endpoint="https://push.example/a", p256dh="p", auth="a"
    )
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch(
        "apps.monitoring.notifications.send_web_push", return_value=True
    ) as m:
        send_alert_notifications(_alert(s))
    assert len(mail.outbox) == 1
    assert m.call_count == 1


@pytest.mark.django_db
def test_push_without_device_only_emails(settings):
    settings.ALERT_EMAIL_ENABLED = True
    settings.ALERT_WEBPUSH_ENABLED = True
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    mail.outbox = []
    _admin("b", User.NotifyChannel.PUSH)  # no device → email fallback
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch(
        "apps.monitoring.notifications.send_web_push", return_value=True
    ) as m:
        send_alert_notifications(_alert(s))
    assert len(mail.outbox) == 1
    assert m.call_count == 0


@pytest.mark.django_db
def test_webpush_disabled_skips_push(settings):
    settings.ALERT_EMAIL_ENABLED = False
    settings.ALERT_WEBPUSH_ENABLED = False
    u = _admin("c", User.NotifyChannel.PUSH)
    PushSubscription.objects.create(
        user=u, endpoint="https://push.example/c", p256dh="p", auth="a"
    )
    s = Station.objects.create(name="OE5A", callsign="OE5A")
    with mock.patch(
        "apps.monitoring.notifications.send_web_push", return_value=True
    ) as m:
        send_alert_notifications(_alert(s))
    assert m.call_count == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notification_dispatch_webpush.py -v`
Expected: FAIL (`ImportError: cannot import name 'send_web_push'` — not yet referenced).

- [ ] **Step 3: Implement the channel**

In `apps/monitoring/notifications.py`, add the import near the top (module level is fine — `apps.webpush` is always installed):

```python
from apps.webpush.dispatch import send_web_push
```

Extend `send_alert_notifications`:

```python
def send_alert_notifications(alert):
    """Dispatch alert via configured channels."""
    if getattr(settings, "ALERT_EMAIL_ENABLED", False):
        _send_email_notification(alert)
    if getattr(settings, "ALERT_TELEGRAM_ENABLED", False):
        _send_telegram_notification(alert)
    if getattr(settings, "ALERT_WEBPUSH_ENABLED", False):
        _send_webpush_notification(alert)
```

Change `_send_email_notification` to use the new email function (replace the import + call):

```python
    if recipients_qs is None:
        from apps.monitoring.recipients import email_recipients_for_station_alert

        recipients_qs = email_recipients_for_station_alert(alert.station)
```

Add the new function (after `_send_email_notification`):

```python
def _send_webpush_notification(alert):
    """Deliver the alert as Web-Push to PUSH/BOTH users with a device.

    Each subscription is sent in isolation so one dead endpoint never
    blocks the rest (send_web_push prunes 404/410 itself).
    """
    from apps.monitoring.recipients import push_recipients_for_station_alert

    payload = {
        "title": f"[OE5XRX] {alert.get_severity_display()}: {alert.title}",
        "body": f"{alert.station.name}: {alert.message}",
        "url": f"/monitoring/alerts/{alert.pk}/",
        "severity": alert.severity,
    }

    count = 0
    for user in push_recipients_for_station_alert(alert.station):
        for subscription in user.push_subscriptions.all():
            if send_web_push(subscription, payload):
                count += 1
    logger.info("Alert web-push delivered to %d subscription(s).", count)
```

> Note: verify the alert-detail URL pattern name/path in `apps/monitoring/urls.py`; if the real path differs from `/monitoring/alerts/<pk>/`, use the correct one (or `reverse("monitoring:alert_detail", args=[alert.pk])`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notification_dispatch_webpush.py tests/test_notification_dispatch.py -v`
Expected: PASS (new webpush wiring + existing email dispatch tests).

- [ ] **Step 5: Commit**

```bash
git add apps/monitoring/notifications.py tests/test_notification_dispatch_webpush.py
git commit -m "feat(monitoring): web-push as third alert channel"
```

---

### Task 8: Service Worker + Manifest views + icons

**Files:**
- Create: `apps/webpush/views.py`
- Create: `apps/webpush/urls.py`
- Create: `apps/webpush/templates/webpush/sw.js`
- Modify: `config/urls.py` (add root-level include, OUTSIDE `i18n_patterns`)
- Create: `static/webpush/icon-192.png`, `static/webpush/icon-512.png` (generated)
- Test: `tests/test_webpush_pwa_assets.py`

**Interfaces:**
- Produces: routes `webpush:service_worker` → `/sw.js`, `webpush:manifest` → `/manifest.webmanifest`. Both root-scoped (no locale prefix).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webpush_pwa_assets.py
import json

import pytest


@pytest.mark.django_db
def test_service_worker_served_at_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r["Content-Type"]
    assert r["Service-Worker-Allowed"] == "/"


@pytest.mark.django_db
def test_manifest_served_at_root(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r["Content-Type"].startswith("application/manifest+json")
    data = json.loads(r.content)
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    assert data["icons"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webpush_pwa_assets.py -v`
Expected: FAIL (404 — routes don't exist).

- [ ] **Step 3: Generate the icons**

Run this one-off (Pillow is already a dependency) to create the two PNGs — orange rounded square with "5X", matching the existing favicon:

```bash
python - <<'PY'
from PIL import Image, ImageDraw, ImageFont
for size in (192, 512):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * 0.22)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(255, 138, 61, 255))
    try:
        font = ImageFont.truetype("DejaVuSansMono-Bold.ttf", int(size * 0.42))
    except OSError:
        font = ImageFont.load_default()
    text = "5X"
    box = d.textbbox((0, 0), text, font=font)
    d.text(
        ((size - (box[2] - box[0])) / 2, (size - (box[3] - box[1])) / 2 - box[1]),
        text, font=font, fill=(18, 10, 4, 255),
    )
    img.save(f"static/webpush/icon-{size}.png")
    print("wrote", size)
PY
```

(Create the `static/webpush/` directory first if needed. These are placeholder brand icons — a designer can replace them later without code changes.)

- [ ] **Step 4: Implement the views + SW template + routes**

`apps/webpush/views.py` (SW + manifest portion; subscribe API added in Task 9):

```python
"""PWA asset views (service worker + manifest) and subscription API.

The service worker and manifest are served through Django rather than
static files because WhiteNoise's ManifestStaticFilesStorage hashes
filenames — a service worker needs a stable URL and root scope.
"""

from django.conf import settings
from django.http import JsonResponse
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_GET
from django.shortcuts import render


@require_GET
def service_worker(request):
    response = render(request, "webpush/sw.js", content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def manifest(request):
    data = {
        "name": "OE5XRX Station Manager",
        "short_name": "OE5XRX",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#120A04",
        "theme_color": "#FF8A3D",
        "icons": [
            {"src": static("webpush/icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": static("webpush/icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")
```

`apps/webpush/templates/webpush/sw.js`:

```javascript
// OE5XRX Station Manager — service worker (push only, no offline cache).
self.addEventListener('push', function (event) {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  const title = data.title || 'OE5XRX';
  const options = {
    body: data.body || '',
    icon: '/static/webpush/icon-192.png',
    badge: '/static/webpush/icon-192.png',
    data: { url: data.url || '/' },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function (list) {
      for (const c of list) { if (c.url.includes(url) && 'focus' in c) return c.focus(); }
      if (clients.openWindow) return clients.openWindow(url);
    })
  );
});
```

`apps/webpush/urls.py`:

```python
from django.urls import path

from . import views

app_name = "webpush"

urlpatterns = [
    path("sw.js", views.service_worker, name="service_worker"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
```

In `config/urls.py`, add to the top-level `urlpatterns` (NOT inside `i18n_patterns`, so the URLs stay locale-free) — e.g. after the `i18n/` line:

```python
    path("", include("apps.webpush.urls")),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_webpush_pwa_assets.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/webpush/views.py apps/webpush/urls.py apps/webpush/templates config/urls.py static/webpush tests/test_webpush_pwa_assets.py
git commit -m "feat(webpush): serve service worker + manifest + PWA icons"
```

---

### Task 9: Subscribe / Unsubscribe API

**Files:**
- Modify: `apps/webpush/views.py` (add subscribe/unsubscribe views)
- Modify: `apps/webpush/urls.py` (add routes)
- Test: `tests/test_webpush_subscribe_api.py`

**Interfaces:**
- Consumes: `PushSubscription` (Task 2).
- Produces: `POST /webpush/subscribe/` (JSON body `{endpoint, keys:{p256dh, auth}}`; login-required; upsert by endpoint; returns `{"ok": true}`), `POST /webpush/unsubscribe/` (JSON body `{endpoint}`; deletes only the caller's matching subscription). Route names `webpush:subscribe`, `webpush:unsubscribe`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_webpush_subscribe_api.py
import json

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.webpush.models import PushSubscription

SUB = {"endpoint": "https://push.example/z", "keys": {"p256dh": "pp", "auth": "aa"}}


@pytest.mark.django_db
def test_subscribe_requires_login(client):
    r = client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert r.status_code in (302, 403)


@pytest.mark.django_db
def test_subscribe_creates_then_upserts(client):
    u = User.objects.create_user(username="a", password="x", email="a@x")
    client.force_login(u)
    r = client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert PushSubscription.objects.filter(user=u, endpoint=SUB["endpoint"]).count() == 1
    # second POST same endpoint → update, not duplicate
    client.post(
        reverse("webpush:subscribe"), data=json.dumps(SUB), content_type="application/json"
    )
    assert PushSubscription.objects.filter(endpoint=SUB["endpoint"]).count() == 1


@pytest.mark.django_db
def test_unsubscribe_only_removes_own(client):
    owner = User.objects.create_user(username="o", password="x", email="o@x")
    other = User.objects.create_user(username="p", password="x", email="p@x")
    PushSubscription.objects.create(
        user=owner, endpoint=SUB["endpoint"], p256dh="pp", auth="aa"
    )
    client.force_login(other)
    r = client.post(
        reverse("webpush:unsubscribe"),
        data=json.dumps({"endpoint": SUB["endpoint"]}),
        content_type="application/json",
    )
    assert r.status_code == 200
    # other user's request must NOT delete owner's subscription
    assert PushSubscription.objects.filter(endpoint=SUB["endpoint"]).exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_webpush_subscribe_api.py -v`
Expected: FAIL (route names don't exist).

- [ ] **Step 3: Implement subscribe/unsubscribe**

Add to `apps/webpush/views.py`:

```python
import json

from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from .models import PushSubscription


@login_required
@require_POST
def subscribe(request):
    try:
        body = json.loads(request.body)
        endpoint = body["endpoint"]
        keys = body["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False, "error": "invalid payload"}, status=400)

    label = request.META.get("HTTP_USER_AGENT", "")[:120]
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh,
            "auth": auth,
            "label": label,
            "failure_count": 0,
        },
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def unsubscribe(request):
    try:
        endpoint = json.loads(request.body)["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False, "error": "invalid payload"}, status=400)

    # Scoped to the caller — a user can only remove their own subscription.
    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})
```

Add to `apps/webpush/urls.py` `urlpatterns`:

```python
    path("webpush/subscribe/", views.subscribe, name="subscribe"),
    path("webpush/unsubscribe/", views.unsubscribe, name="unsubscribe"),
```

> `update_or_create(endpoint=...)` re-homes an endpoint to the current user if it moved browsers/accounts — endpoints are globally unique, so this is correct upsert behaviour, not a hijack.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_webpush_subscribe_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/webpush/views.py apps/webpush/urls.py tests/test_webpush_subscribe_api.py
git commit -m "feat(webpush): subscribe/unsubscribe API"
```

---

### Task 10: Notification-Settings page (channel + devices + push JS)

**Files:**
- Create: `apps/accounts/forms_notifications.py` (or extend existing `forms.py` — check convention)
- Create: `apps/accounts/views_notifications.py`
- Create: `templates/accounts/notification_settings.html`
- Create: `static/webpush/push.js`
- Modify: `apps/accounts/urls.py` (add route)
- Test: `tests/test_notification_settings_page.py`

**Interfaces:**
- Consumes: `User.notify_channel` (Task 5), `webpush:subscribe`/`unsubscribe` routes (Task 9), `settings.WEBPUSH_VAPID_PUBLIC_KEY` (Task 1).
- Produces: route `accounts:notification_settings` → `/accounts/notifications/`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notification_settings_page.py
import pytest
from django.urls import reverse

from apps.accounts.models import User


@pytest.mark.django_db
def test_page_requires_login(client):
    r = client.get(reverse("accounts:notification_settings"))
    assert r.status_code == 302


@pytest.mark.django_db
def test_post_updates_channel(client):
    u = User.objects.create_user(username="a", password="x", email="a@x")
    client.force_login(u)
    r = client.post(
        reverse("accounts:notification_settings"), data={"notify_channel": "both"}
    )
    assert r.status_code in (200, 302)
    u.refresh_from_db()
    assert u.notify_channel == User.NotifyChannel.BOTH
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_notification_settings_page.py -v`
Expected: FAIL (route doesn't exist).

- [ ] **Step 3: Implement form + view + route**

`apps/accounts/forms_notifications.py`:

```python
from django import forms

from .models import User


class NotificationChannelForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["notify_channel"]
        widgets = {"notify_channel": forms.RadioSelect}
```

`apps/accounts/views_notifications.py`:

```python
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import UpdateView

from .forms_notifications import NotificationChannelForm


class NotificationSettingsView(LoginRequiredMixin, UpdateView):
    form_class = NotificationChannelForm
    template_name = "accounts/notification_settings.html"
    success_url = reverse_lazy("accounts:notification_settings")

    def get_object(self):
        return self.request.user

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["vapid_public_key"] = settings.WEBPUSH_VAPID_PUBLIC_KEY
        ctx["webpush_enabled"] = settings.ALERT_WEBPUSH_ENABLED
        ctx["subscriptions"] = self.request.user.push_subscriptions.all()
        return ctx
```

In `apps/accounts/urls.py`, import and add the route (near the `profile/` routes):

```python
from .views_notifications import NotificationSettingsView
```
```python
    path(
        "notifications/",
        NotificationSettingsView.as_view(),
        name="notification_settings",
    ),
```

- [ ] **Step 4: Create the template**

`templates/accounts/notification_settings.html` (extends the project base — verify the exact base template name/blocks used by `templates/accounts/profile.html` and mirror them). Key parts — channel form, iOS install hint, device list, and the push button wired to `push.js` via data-attributes:

```django
{% extends "base.html" %}
{% load i18n static %}

{% block title %}{% trans "Notification settings" %}{% endblock %}

{% block content %}
<h1>{% trans "Notifications" %}</h1>

<form method="post">
  {% csrf_token %}
  {{ form.as_p }}
  <button type="submit" class="btn btn-primary">{% trans "Save" %}</button>
</form>

{% if webpush_enabled %}
  <section id="push-section"
           data-vapid-key="{{ vapid_public_key }}"
           data-subscribe-url="{% url 'webpush:subscribe' %}"
           data-unsubscribe-url="{% url 'webpush:unsubscribe' %}">
    <h2>{% trans "Push on this device" %}</h2>

    {% comment %}
      iOS only allows Web-Push for an INSTALLED PWA (iOS 16.4+). The user
      must Add-to-Home-Screen first, then open the app from the home screen.
    {% endcomment %}
    <p class="hint">
      {% blocktrans %}On iPhone/iPad: first use Share → Add to Home Screen,
      then open the app from the home screen and enable push here.{% endblocktrans %}
    </p>

    <button type="button" id="enable-push" class="btn btn-secondary">
      {% trans "Enable push on this device" %}
    </button>
    <p id="push-status" role="status"></p>

    <h3>{% trans "Registered devices" %}</h3>
    <ul>
      {% for s in subscriptions %}
        <li>{{ s.label|default:s.endpoint|truncatechars:40 }} — {{ s.created_at|date }}</li>
      {% empty %}
        <li>{% trans "No devices registered." %}</li>
      {% endfor %}
    </ul>
  </section>
  <script src="{% static 'webpush/push.js' %}" nonce="{{ csp_nonce }}" defer></script>
{% endif %}
{% endblock %}
```

`static/webpush/push.js`:

```javascript
// Registers this browser for Web-Push and POSTs the subscription.
(function () {
  const section = document.getElementById('push-section');
  const btn = document.getElementById('enable-push');
  const status = document.getElementById('push-status');
  if (!section || !btn) return;

  function getCookie(name) {
    const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
    return m ? m.pop() : '';
  }

  function urlB64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
  }

  btn.addEventListener('click', async function () {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      status.textContent = 'Push is not supported on this browser.';
      return;
    }
    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { status.textContent = 'Permission denied.'; return; }
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(section.dataset.vapidKey),
      });
      const res = await fetch(section.dataset.subscribeUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
        body: JSON.stringify(sub.toJSON()),
      });
      status.textContent = res.ok ? 'Push enabled on this device.' : 'Registration failed.';
      if (res.ok) setTimeout(() => location.reload(), 800);
    } catch (e) {
      status.textContent = 'Could not enable push: ' + e.message;
    }
  });
})();
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_notification_settings_page.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/forms_notifications.py apps/accounts/views_notifications.py apps/accounts/urls.py templates/accounts/notification_settings.html static/webpush/push.js tests/test_notification_settings_page.py
git commit -m "feat(accounts): notification-settings page with push opt-in"
```

---

### Task 11: PWA head + service-worker registration in base.html

**Files:**
- Modify: `templates/base.html` (head, before `{% block extra_head %}` at ~21)
- Modify: `templates/includes/sidebar.html` (add a nav link to notification settings — verify the file; mirror existing nav-item markup)
- Test: `tests/test_pwa_base_head.py`

**Interfaces:**
- Consumes: `webpush:manifest` route (Task 8).
- Produces: manifest `<link>`, Apple PWA meta tags, and a nonce'd SW-registration script in every page.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pwa_base_head.py
import pytest


@pytest.mark.django_db
def test_base_head_has_manifest_and_sw_registration(client):
    r = client.get("/accounts/login/")
    html = r.content.decode()
    assert 'rel="manifest"' in html
    assert "/manifest.webmanifest" in html
    assert "serviceWorker" in html
    assert 'name="apple-mobile-web-app-capable"' in html
```

(If `/accounts/login/` isn't the right unauthenticated page, use whatever public page renders `base.html`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pwa_base_head.py -v`
Expected: FAIL (tags absent).

- [ ] **Step 3: Add PWA head + registration**

In `templates/base.html`, immediately before `{% block extra_head %}{% endblock %}` (~21):

```django
  <link rel="manifest" href="{% url 'webpush:manifest' %}">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="OE5XRX">
  <meta name="theme-color" content="#FF8A3D">
  <link rel="apple-touch-icon" href="{% static 'webpush/icon-192.png' %}">
  <script nonce="{{ csp_nonce }}">
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', function () {
        navigator.serviceWorker.register('/sw.js').catch(function () {});
      });
    }
  </script>
```

(`{% load static %}` is already active at the top of `base.html`.)

- [ ] **Step 4: Add the nav link**

In `templates/includes/sidebar.html`, add a link to `{% url 'accounts:notification_settings' %}` alongside the existing profile/nav items (mirror the surrounding markup + i18n `{% trans %}` label "Notifications").

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_pwa_base_head.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add templates/base.html templates/includes/sidebar.html tests/test_pwa_base_head.py
git commit -m "feat(webpush): PWA head tags + service-worker registration"
```

---

### Task 12: Full-suite verification + docs note

**Files:**
- Modify: `CLAUDE.md` (station-manager architecture section — add a short webpush note)
- No new tests (integration verification only)

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: PASS (no regressions in monitoring/accounts/other apps).

- [ ] **Step 2: Run the template-comment guard + linters**

Run the project's lint/guard commands (check `justfile`): e.g. `ruff check .` and the Django template-comment guard.
Expected: clean.

- [ ] **Step 3: Add a CLAUDE.md architecture note**

Under "Architektur — station-manager", add a short subsection documenting: `apps/webpush` provides PWA install + Web-Push as a third alert channel; `notify_channel` on `User` drives per-user routing; PUSH-without-device falls back to e-mail; SW/manifest are served via Django views (not static) because WhiteNoise hashes filenames; iOS needs an installed PWA (16.4+).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(webpush): architecture note for PWA + web-push"
```

---

## Self-Review

**Spec coverage:**
- PWA installable (manifest, icons, apple meta, standalone) → Tasks 8, 11. ✓
- Web-Push third channel → Tasks 4, 7. ✓
- Per-user preference EMAIL/PUSH/BOTH → Tasks 5, 10. ✓
- E-mail fallback for PUSH-without-device → Tasks 6, 7 (tests pin it). ✓
- VAPID key handling, feature-flag-off-without-keys → Tasks 1, 3. ✓
- SW/manifest via Django views (WhiteNoise hashing) → Task 8. ✓
- Subscribe/unsubscribe, auth + own-only → Task 9. ✓
- Expiry pruning (404/410), isolated per-subscription failures → Tasks 4, 7. ✓
- iOS install constraint surfaced in UI → Task 10. ✓
- Security (auth, CSRF, private key in env) → Tasks 1, 9; CSP unchanged (Global Constraints). ✓
- Testing plan items 1–5 from spec → Tasks 4, 6, 7, 8, 9. ✓

**Placeholder scan:** No TBD/TODO; every code + test block is concrete. Two "verify the exact name" notes (alert-detail URL in Task 7, base template blocks / sidebar markup in Tasks 10–11) are deliberate codebase-lookup instructions, not deferred implementation.

**Type consistency:** `send_web_push(subscription, payload)` defined Task 4, consumed Task 7. `email_recipients_for_station_alert` / `push_recipients_for_station_alert` defined Task 6, consumed Task 7. `User.NotifyChannel` / `notify_channel` defined Task 5, consumed Tasks 6, 10. `PushSubscription.subscription_info` defined Task 2, consumed Task 4. Route names `webpush:manifest|service_worker|subscribe|unsubscribe`, `accounts:notification_settings` consistent across Tasks 8–11. ✓
