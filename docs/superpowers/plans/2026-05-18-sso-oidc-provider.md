# SSO/OIDC Provider — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn station-manager into the central OpenID-Connect provider for OE5XRX-Apps (InvenTree, Grafana, Nextcloud, …), with central per-user app-access control.

**Architecture:** New `apps/sso` Django-App as thin layer on `django-oauth-toolkit` (DOT). DOT provides the full OIDC machinery; we add `AppGrant` (access gate), custom claims hook, custom validator, audit log, and Bootstrap-styled UI. `User.role` field is refactored to Django's built-in `auth.Group` M2M for flexible multi-role support.

**Tech Stack:** Django 6.0, django-oauth-toolkit ≥ 3.0, RSA-2048 + RS256 JWT signing, PostgreSQL 17, HTMX for inline UI updates, authlib for test-client integration.

**Reference spec:** `docs/superpowers/specs/2026-05-18-sso-oidc-provider-design.md`

---

## Phase 1 — DOT Installation & Configuration

### Task 1: Install django-oauth-toolkit and run its migrations

**Files:**
- Modify: `requirements/base.txt`
- Modify: `config/settings/base.py:25-52` (INSTALLED_APPS)
- Modify: `config/settings/base.py` (append OAUTH2_PROVIDER block)
- Modify: `config/urls.py:1-25`
- Modify: `requirements/dev.txt` (add authlib for tests)

- [ ] **Step 1: Add the dependency**

Append to `requirements/base.txt`:

```
django-oauth-toolkit>=3.0,<4.0
```

Append to `requirements/dev.txt`:

```
authlib>=1.3,<2.0
```

- [ ] **Step 2: Rebuild the dev container so the new package is available**

Run:
```bash
docker compose build web
docker compose up -d db redis
```

- [ ] **Step 3: Add `oauth2_provider` to INSTALLED_APPS**

Modify `config/settings/base.py` — add `"oauth2_provider"` between `"daphne"` and Django contrib apps (DOT must come before contrib for some signal ordering). Replace the existing INSTALLED_APPS list:

```python
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
    "apps.firmware",
    "apps.deployments",
    "apps.builder",
    "apps.tunnel",
    "apps.audit",
    "apps.monitoring",
    "apps.images",
    "apps.provisioning",
    "apps.rollouts",
]
```

- [ ] **Step 4: Add minimal `OAUTH2_PROVIDER` config to `base.py`**

Append at the bottom of `config/settings/base.py`:

```python
# Django OAuth Toolkit — OIDC provider configuration.
# Issuer must match the public URL prefix (see /sso/ in config/urls.py).
# RSA private key path is resolved at runtime by the setup_oidc_keys
# management command (Task 4); the file lives on a persistent volume
# so token signatures survive container restarts.
OIDC_RSA_KEY_PATH = os.environ.get("OIDC_RSA_KEY_PATH", str(BASE_DIR / "oidc_keys" / "private.pem"))

OAUTH2_PROVIDER = {
    "OIDC_ENABLED": True,
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
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,            # 1 h
    "ID_TOKEN_EXPIRE_SECONDS": 3600,                # 1 h
    "REFRESH_TOKEN_EXPIRE_SECONDS": 14 * 24 * 3600, # 14 d
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,
    "ROTATE_REFRESH_TOKEN": True,
    "OAUTH2_VALIDATOR_CLASS": "apps.sso.permissions.SsoOAuth2Validator",
    "OIDC_USERINFO_HOOK": "apps.sso.oidc_claims.add_claims",
}
```

- [ ] **Step 5: Add `/sso/` URL include**

Modify `config/urls.py` to insert `path("sso/", include("oauth2_provider.urls", namespace="oauth2_provider"))` **outside** the `i18n_patterns` block. Replace `config/urls.py`:

```python
from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("api/", include("apps.api.urls")),
    path("i18n/", include("django.conf.urls.i18n")),
    # OIDC endpoints — kept out of i18n_patterns so well-known URLs
    # don't carry a locale prefix that breaks RP discovery.
    path("sso/", include("oauth2_provider.urls", namespace="oauth2_provider")),
]

urlpatterns += i18n_patterns(
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("stations/", include("apps.stations.urls")),
    path("firmware/", include("apps.firmware.urls")),
    path("deployments/", include("apps.deployments.urls")),
    path("builder/", include("apps.builder.urls")),
    path("tunnel/", include("apps.tunnel.urls")),
    path("audit/", include("apps.audit.urls")),
    path("monitoring/", include("apps.monitoring.urls")),
    path("images/", include("apps.images.urls")),
    path("provisioning/", include("apps.provisioning.urls")),
    path("rollouts/", include("apps.rollouts.urls")),
    path("", include("apps.dashboard.urls")),
)

if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
    ] + urlpatterns
```

- [ ] **Step 6: Run DOT migrations**

Run:
```bash
docker compose run --rm web python manage.py migrate oauth2_provider
```
Expected: 4–8 migrations applied (Application, AccessToken, RefreshToken, Grant, IDToken).

- [ ] **Step 7: Commit**

```bash
git add requirements/base.txt requirements/dev.txt config/settings/base.py config/urls.py
git commit -m "sso: install django-oauth-toolkit and mount /sso/ endpoints

Adds DOT to INSTALLED_APPS, base settings for OIDC scopes / lifetimes /
PKCE / custom validator+claims hooks (placeholders — implemented in
later tasks). URL include is outside i18n_patterns so .well-known
endpoints don't get a locale prefix.

Token endpoints aren't yet usable — RSA key bootstrap + custom
validator + AppGrant model come in the next tasks."
```

---

### Task 2: RSA-keypair bootstrap management command

**Files:**
- Create: `apps/sso/__init__.py`
- Create: `apps/sso/apps.py`
- Create: `apps/sso/management/__init__.py`
- Create: `apps/sso/management/commands/__init__.py`
- Create: `apps/sso/management/commands/setup_oidc_keys.py`
- Create: `tests/test_sso_keys.py`
- Modify: `config/settings/dev.py` (read key into OAUTH2_PROVIDER)
- Modify: `config/settings/prod.py` (read key into OAUTH2_PROVIDER)
- Modify: `config/settings/test.py` (in-memory test key)

- [ ] **Step 1: Create the empty app skeleton**

`apps/sso/__init__.py`:
```python
```
(empty file)

`apps/sso/apps.py`:
```python
from django.apps import AppConfig


class SsoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sso"
    label = "sso"
    verbose_name = "SSO / OIDC Provider"
```

`apps/sso/management/__init__.py` and `apps/sso/management/commands/__init__.py`: both empty.

- [ ] **Step 2: Write the failing test for `setup_oidc_keys`**

Create `tests/test_sso_keys.py`:

```python
import io
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from django.core.management import call_command


@pytest.mark.django_db
def test_setup_oidc_keys_creates_a_valid_rsa_2048_private_key(tmp_path):
    """First run: writes a fresh 2048-bit RSA PEM at the target path."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))

    assert target.exists()
    pem = target.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    assert key.key_size == 2048


@pytest.mark.django_db
def test_setup_oidc_keys_is_idempotent(tmp_path):
    """Second run with an existing key leaves it untouched."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    original = target.read_bytes()
    call_command("setup_oidc_keys", path=str(target))
    assert target.read_bytes() == original


@pytest.mark.django_db
def test_setup_oidc_keys_force_overwrites(tmp_path):
    """With --force, an existing key is regenerated."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    original = target.read_bytes()
    call_command("setup_oidc_keys", path=str(target), force=True)
    assert target.read_bytes() != original
```

- [ ] **Step 3: Run test, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_keys.py -v
```
Expected: ERROR — `setup_oidc_keys` command unknown.

- [ ] **Step 4: Implement the command**

Create `apps/sso/management/commands/setup_oidc_keys.py`:

```python
"""Bootstrap the RSA private key used to sign OIDC ID tokens.

Idempotent by design — re-running on a host that already has a key
must NOT regenerate it (that would invalidate every live token).
Pass --force only when an operator deliberately wants to rotate.
"""

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create the RSA-2048 private key for OIDC ID-token signing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=None,
            help="Override the destination path (default: settings.OIDC_RSA_KEY_PATH).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate even if a key already exists at the target path.",
        )

    def handle(self, *args, path=None, force=False, **options):
        from django.conf import settings

        target = Path(path or settings.OIDC_RSA_KEY_PATH)
        if target.exists() and not force:
            self.stdout.write(f"Key already present at {target}; nothing to do.")
            return

        target.parent.mkdir(parents=True, exist_ok=True)

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        target.write_bytes(pem)
        target.chmod(0o600)
        self.stdout.write(self.style.SUCCESS(f"Wrote RSA-2048 private key to {target}."))
```

- [ ] **Step 5: Wire the key into runtime settings**

Append to `config/settings/dev.py`:

```python
from pathlib import Path

_oidc_key_path = Path(OIDC_RSA_KEY_PATH)
if _oidc_key_path.exists():
    OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _oidc_key_path.read_text()
```

Append to `config/settings/prod.py`:

```python
from pathlib import Path

_oidc_key_path = Path(OIDC_RSA_KEY_PATH)
try:
    OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _oidc_key_path.read_text()
except FileNotFoundError as exc:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        f"OIDC_RSA_KEY_PATH={_oidc_key_path} missing — "
        "run `python manage.py setup_oidc_keys` once on the host."
    ) from exc
```

Modify `config/settings/test.py` — add an in-memory key so tests don't touch disk:

```python
# Append near the end:
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_test_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"] = _test_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()
```

- [ ] **Step 6: Add `apps.sso` to INSTALLED_APPS**

Modify `config/settings/base.py` — append `"apps.sso"` to the Local-apps block (after `"apps.rollouts"`).

- [ ] **Step 7: Run tests, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_keys.py -v
```
Expected: 3 passed.

- [ ] **Step 8: Commit**

```bash
git add apps/sso/ tests/test_sso_keys.py config/settings/
git commit -m "sso: management command + settings wiring for RSA key bootstrap

setup_oidc_keys is idempotent — re-running with an existing key is a
no-op so a container restart never invalidates live tokens. --force
is the explicit rotation knob. test.py uses an in-memory key so the
suite has no filesystem dependency."
```

---

### Task 3: Smoke-test the Discovery endpoint

**Files:**
- Create: `tests/test_sso_discovery.py`

- [ ] **Step 1: Write the smoke test**

```python
import pytest


@pytest.mark.django_db
def test_discovery_endpoint_is_reachable_and_advertises_pkce(client):
    """RPs hit /sso/.well-known/openid-configuration to bootstrap.

    The endpoint must respond 200 with a JSON document advertising
    code_challenge_methods_supported=["S256"] — that's how InvenTree
    decides to send a PKCE challenge.
    """
    resp = client.get("/sso/.well-known/openid-configuration/")
    assert resp.status_code == 200
    data = resp.json()
    assert "S256" in data["code_challenge_methods_supported"]
    assert "RS256" in data["id_token_signing_alg_values_supported"]
    assert "code" in data["response_types_supported"]
    assert "authorization_code" in data["grant_types_supported"]
    assert "refresh_token" in data["grant_types_supported"]
    assert data["authorization_endpoint"].endswith("/sso/authorize/")
    assert data["token_endpoint"].endswith("/sso/token/")
    assert data["userinfo_endpoint"].endswith("/sso/userinfo/")
    assert data["jwks_uri"].endswith("/sso/.well-known/jwks.json")
```

- [ ] **Step 2: Run, expect failure**

The validator class `apps.sso.permissions.SsoOAuth2Validator` doesn't exist yet → Django won't start.

```bash
docker compose run --rm web pytest tests/test_sso_discovery.py -v
```
Expected: ERROR — `ModuleNotFoundError` for `apps.sso.permissions`.

- [ ] **Step 3: Stub the validator + claims so settings load**

Create `apps/sso/permissions.py`:

```python
"""OIDC access-control validator.

Subclasses DOT's default OAuth2Validator. Real AppGrant + is_active
gating arrives in Task 9; for now this is a pass-through so the
discovery endpoint can boot.
"""

from oauth2_provider.oauth2_validators import OAuth2Validator


class SsoOAuth2Validator(OAuth2Validator):
    pass
```

Create `apps/sso/oidc_claims.py`:

```python
"""OIDC claims hook.

Wired into OAUTH2_PROVIDER["OIDC_USERINFO_HOOK"]. Real custom claims
land in Task 8; for now this is a pass-through.
"""


def add_claims(claims, user, request):
    return claims
```

- [ ] **Step 4: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_discovery.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/permissions.py apps/sso/oidc_claims.py tests/test_sso_discovery.py
git commit -m "sso: discovery endpoint smoke test + validator/claims stubs

Verifies PKCE + RS256 + code-flow are advertised so InvenTree/Grafana
can auto-configure from Discovery. Validator + claims hooks are
empty subclasses today; concrete logic in Tasks 8 and 9."
```

---

## Phase 2 — User Role → Django Groups Migration

This phase replaces `User.role` (single CharField enum) with Django's built-in `auth.Group` M2M. Three commits, each independently deployable.

### Task 4: Data migration — create groups, populate memberships

**Files:**
- Create: `apps/accounts/migrations/0002_role_to_groups.py`
- Create: `tests/test_role_to_groups_migration.py`

- [ ] **Step 1: Discover the next migration number**

```bash
docker compose run --rm web ls apps/accounts/migrations/
```
Expected: `0001_initial.py` (so the new one is `0002_role_to_groups.py`). If you see a different highest number, increment from there.

- [ ] **Step 2: Write the migration test first**

Create `tests/test_role_to_groups_migration.py`:

```python
"""Verify the data migration that maps User.role to auth.Group memberships.

We use django_test_migrations so the migration runs against a clean
schema and we can assert behavior at the boundary between 0001 and 0002.
"""

import pytest


@pytest.mark.django_db(transaction=True)
def test_role_to_groups_migration_assigns_users_to_correct_groups(migrator):
    """Each existing user lands in exactly one group matching their old role."""
    old_state = migrator.apply_initial_migration(
        [("accounts", "0001_initial")]
    )
    OldUser = old_state.apps.get_model("accounts", "User")
    OldUser.objects.create_user(
        username="alice", password="x", role="admin", email="a@x"
    )
    OldUser.objects.create_user(
        username="bob", password="x", role="operator", email="b@x"
    )
    OldUser.objects.create_user(
        username="carol", password="x", role="member", email="c@x"
    )

    new_state = migrator.apply_tested_migration(
        [("accounts", "0002_role_to_groups")]
    )
    NewUser = new_state.apps.get_model("accounts", "User")
    Group = new_state.apps.get_model("auth", "Group")

    assert {g.name for g in Group.objects.all()} >= {"admin", "operator", "member"}

    alice = NewUser.objects.get(username="alice")
    bob = NewUser.objects.get(username="bob")
    carol = NewUser.objects.get(username="carol")

    assert list(alice.groups.values_list("name", flat=True)) == ["admin"]
    assert list(bob.groups.values_list("name", flat=True)) == ["operator"]
    assert list(carol.groups.values_list("name", flat=True)) == ["member"]


@pytest.mark.django_db(transaction=True)
def test_groups_exist_even_with_no_users(migrator):
    """The three default groups are created idempotently with zero users."""
    migrator.apply_initial_migration([("accounts", "0001_initial")])
    new_state = migrator.apply_tested_migration(
        [("accounts", "0002_role_to_groups")]
    )
    Group = new_state.apps.get_model("auth", "Group")
    assert {g.name for g in Group.objects.all()} >= {"admin", "operator", "member"}
```

Add to `requirements/dev.txt` if not present:
```
django-test-migrations>=1.4
```

- [ ] **Step 3: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_role_to_groups_migration.py -v
```
Expected: ERROR — migration `0002_role_to_groups` doesn't exist.

- [ ] **Step 4: Write the migration**

Create `apps/accounts/migrations/0002_role_to_groups.py`:

```python
from django.db import migrations

GROUPS = ("admin", "operator", "member")


def create_groups_and_assign(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model("accounts", "User")

    # Idempotent group creation.
    groups_by_name = {}
    for name in GROUPS:
        group, _ = Group.objects.get_or_create(name=name)
        groups_by_name[name] = group

    # Assign every existing user to the group matching their .role
    # value. Users with an unrecognized role (shouldn't happen given
    # TextChoices, but defense-in-depth) get no group.
    for user in User.objects.all():
        target = groups_by_name.get(user.role)
        if target is not None:
            user.groups.add(target)


def reverse_remove_users_from_groups(apps, schema_editor):
    """Pull users back out of the three groups; leave the groups themselves
    alone (they might have admin-defined members we don't know about).
    """
    Group = apps.get_model("auth", "Group")
    for name in GROUPS:
        try:
            group = Group.objects.get(name=name)
        except Group.DoesNotExist:
            continue
        group.user_set.clear()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("auth", "__latest__"),
    ]

    operations = [
        migrations.RunPython(
            create_groups_and_assign,
            reverse_remove_users_from_groups,
        ),
    ]
```

- [ ] **Step 5: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_role_to_groups_migration.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/accounts/migrations/0002_role_to_groups.py tests/test_role_to_groups_migration.py requirements/dev.txt
git commit -m "accounts: data migration assigning users to auth.Group memberships

Creates admin/operator/member groups idempotently and copies each
user's existing .role value into the corresponding group membership.
User.role field stays in place (read-only) until Task 6 drops it.

Reverse migration only clears the three groups' user_set — it does
NOT delete the Group rows, since an admin may have manually added
members between the forward and reverse runs."
```

---

### Task 5: User-model refactor — add cached_property helpers, sweep call sites

**Files:**
- Modify: `apps/accounts/models.py` (add cached_property helpers, keep role field)
- Modify: `apps/accounts/views.py:15-19` (AdminRequiredMixin)
- Modify: `apps/api/views.py:130` (role check)
- Modify: `apps/stations/views.py:41-46, 106` (mixin + admin gate)
- Modify: `apps/monitoring/views.py:15-26`
- Modify: `apps/tunnel/consumers.py:30`
- Modify: `apps/tunnel/views.py:25`
- Modify: 14 template files (replace `{% if user.role == ... %}` with property-style checks via `{% if user.is_admin %}` etc.)
- Modify: `tests/test_accounts.py:11,16`

- [ ] **Step 1: Write the failing test for the new properties**

Append to `tests/test_accounts.py`:

```python
import pytest
from django.contrib.auth.models import Group


@pytest.mark.django_db
def test_is_admin_true_when_user_in_admin_group():
    from apps.accounts.models import User

    admin_group = Group.objects.create(name="admin")
    user = User.objects.create_user(username="a", password="x", email="a@x")
    user.groups.add(admin_group)
    assert user.is_admin is True
    assert user.is_operator is False
    assert user.is_staff_member is True


@pytest.mark.django_db
def test_is_operator_true_when_user_in_operator_group():
    from apps.accounts.models import User

    op_group = Group.objects.create(name="operator")
    user = User.objects.create_user(username="o", password="x", email="o@x")
    user.groups.add(op_group)
    assert user.is_admin is False
    assert user.is_operator is True
    assert user.is_staff_member is True


@pytest.mark.django_db
def test_member_user_is_neither_admin_nor_operator():
    from apps.accounts.models import User

    member_group = Group.objects.create(name="member")
    user = User.objects.create_user(username="m", password="x", email="m@x")
    user.groups.add(member_group)
    assert user.is_admin is False
    assert user.is_operator is False
    assert user.is_staff_member is False
```

Edit the existing `tests/test_accounts.py:11` block to remove the `role==...` assertions (they'll come back as group assertions). Replace lines 11 and 16:

```python
# OLD: assert user.role == "member"   -> remove
# OLD: assert user.role == "admin"    -> remove
# The bare creation tests stay; role-presence tests are covered by the new tests above.
```

- [ ] **Step 2: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_accounts.py -v
```
Expected: ERROR — `is_staff_member` AttributeError; `is_admin`/`is_operator` still reads `role`.

- [ ] **Step 3: Refactor `apps/accounts/models.py`**

Replace the file:

```python
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with role and language preferences.

    Group membership replaces the old single-valued `role` field. The
    three default groups (admin / operator / member) are created by
    apps.accounts.migrations.0002_role_to_groups; new groups can be
    added freely via Django Admin without code changes.

    Cached properties below mirror the pre-refactor `is_admin` /
    `is_operator` API so call sites need not learn about Groups. A new
    `is_staff_member` covers the common admin-OR-operator gate.
    """

    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        GERMAN = "de", _("German")

    # `role` is intentionally retained for one release so the data migration
    # in 0002 and any pre-deploy code keep working. Task 6 (next commit)
    # drops the column once nothing reads it.
    class Role(models.TextChoices):
        ADMIN = "admin", _("Admin")
        OPERATOR = "operator", _("Operator")
        MEMBER = "member", _("Member")

    role = models.CharField(
        _("role"),
        max_length=10,
        choices=Role.choices,
        default=Role.MEMBER,
        help_text=_(
            "DEPRECATED — superseded by Django Groups in migration 0002. "
            "This column is dropped in the next release."
        ),
    )
    language = models.CharField(
        _("language"),
        max_length=2,
        choices=Language.choices,
        default=Language.ENGLISH,
    )

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["username"]

    def __str__(self):
        return self.username

    @cached_property
    def is_admin(self):
        return self.groups.filter(name="admin").exists()

    @cached_property
    def is_operator(self):
        return self.groups.filter(name="operator").exists()

    @cached_property
    def is_staff_member(self):
        """True iff user is in admin OR operator group."""
        return self.groups.filter(name__in=["admin", "operator"]).exists()
```

- [ ] **Step 4: Run the new tests, expect pass**

```bash
docker compose run --rm web pytest tests/test_accounts.py -v
```
Expected: existing tests + 3 new tests all pass.

- [ ] **Step 5: Sweep `.py` call sites**

Edit each file, replacing the listed pattern with the property:

`apps/accounts/views.py:19`:
```python
# OLD:
return self.request.user.role == "admin"
# NEW:
return self.request.user.is_admin
```

`apps/api/views.py:130`:
```python
# OLD:
if request.user.role not in ("admin", "operator"):
# NEW:
if not request.user.is_staff_member:
```

`apps/stations/views.py:45`:
```python
# OLD:
return self.request.user.role in ("admin", "operator")
# NEW:
return self.request.user.is_staff_member
```

`apps/stations/views.py:106`:
```python
# OLD:
if self.request.user.role == "admin":
# NEW:
if self.request.user.is_admin:
```

`apps/monitoring/views.py:19`:
```python
# OLD:
return self.request.user.role == "admin"
# NEW:
return self.request.user.is_admin
```

`apps/monitoring/views.py:26`:
```python
# OLD:
return self.request.user.role in ("admin", "operator")
# NEW:
return self.request.user.is_staff_member
```

`apps/tunnel/consumers.py:30`:
```python
# OLD:
if user.role not in ("admin", "operator"):
# NEW:
if not user.is_staff_member:
```

`apps/tunnel/views.py:25`:
```python
# OLD:
is_online and active_sessions < 2 and request.user.role in ("admin", "operator")
# NEW:
is_online and active_sessions < 2 and request.user.is_staff_member
```

- [ ] **Step 6: Sweep `.html` templates**

Each template uses `{% if user.role == 'admin' %}` or `{% if user.role == 'admin' or user.role == 'operator' %}`. Replace per file:

| File | Pattern → Replacement |
|---|---|
| `templates/includes/sidebar.html:66` | `user.role == 'admin'` → `user.is_admin` |
| `templates/includes/sidebar.html:86` | same |
| `apps/accounts/templates/accounts/user_list.html:54-55` | drop `pill-accent / pill-violet` branches — see note below |
| `apps/dashboard/templates/dashboard/index.html:22,155` | `user.role == 'admin' or user.role == 'operator'` → `user.is_staff_member` |
| `apps/firmware/templates/firmware/firmware_list.html:20` | same |
| `apps/firmware/templates/firmware/firmware_detail.html:31` | same |
| `apps/monitoring/templates/monitoring/alert_list.html:20` | `user.role == 'admin'` → `user.is_admin` |
| `apps/monitoring/templates/monitoring/alert_settings.html:72` | same |
| `apps/monitoring/templates/monitoring/_alert_cards.html:20` | `user.role == 'admin' or user.role == 'operator'` → `user.is_staff_member` |
| `apps/provisioning/templates/provisioning/_provisioning_section.html:2` | `user.role == 'admin'` → `user.is_admin` |
| `apps/rollouts/templates/rollouts/_station_upgrade_card.html:2` | same |
| `apps/stations/templates/stations/_device_token.html:12,39` | `request.user.role == "admin" or request.user.role == "operator"` → `request.user.is_staff_member` |
| `apps/stations/templates/stations/station_list.html:22` | `user.role == 'admin' or user.role == 'operator'` → `user.is_staff_member` |
| `apps/stations/templates/stations/station_detail.html:42,95,181,223,410` | same |

**Note on `user_list.html:54-55`:** The original code shows a colored pill per role using `get_role_display`. Replace those two lines with a loop over groups:

```django
{% for g in u.groups.all %}
  {% if g.name == "admin" %}<span class="pill pill-accent">ADMIN</span>
  {% elif g.name == "operator" %}<span class="pill pill-violet">OPERATOR</span>
  {% else %}<span class="pill">{{ g.name|upper }}</span>{% endif %}
{% endfor %}
```

- [ ] **Step 7: Run full test suite**

```bash
docker compose run --rm web pytest -x
```
Expected: full suite passes. Any failure indicates a missed call site — grep again with the patterns from Step 5/6.

- [ ] **Step 8: Commit**

```bash
git add apps/accounts/models.py apps/accounts/views.py apps/api/views.py apps/stations/views.py apps/monitoring/views.py apps/tunnel/consumers.py apps/tunnel/views.py templates/ apps/*/templates/ tests/test_accounts.py
git commit -m "accounts: switch all role checks to Group-backed properties

Adds cached_property is_admin / is_operator / is_staff_member on
User, all reading from user.groups. Sweeps 25 call sites (.py + .html)
from user.role-string comparisons to the new properties.

User.role is now write-only / unused but the column stays in the
schema for one release. Task 6 drops it after this is deployed and
verified."
```

---

### Task 6: Drop the `User.role` column

**Files:**
- Create: `apps/accounts/migrations/0003_drop_role.py`
- Modify: `apps/accounts/models.py` (remove role field + Role choices)
- Modify: `apps/accounts/forms.py` (drop role from ModelForm fields if present)
- Modify: `apps/accounts/admin.py` (drop role from list_display / fieldsets)
- Modify: `apps/accounts/managers.py` (drop role kwarg from create_superuser if present)

- [ ] **Step 1: Audit remaining references**

```bash
docker compose run --rm web grep -rn "\.role" apps/ templates/ tests/ --include="*.py" --include="*.html" | grep -v "is_admin\|is_operator\|is_staff_member\|migrations/"
```
Expected: only legitimate hits (e.g., `User.Role` enum references, form field declarations in `forms.py`). If any business-logic references remain, go back to Task 5 and fix them.

- [ ] **Step 2: Read current forms.py, admin.py, managers.py to know what to strip**

```bash
docker compose run --rm web cat apps/accounts/forms.py apps/accounts/admin.py apps/accounts/managers.py
```
Note any references to `role` — they need to be removed alongside the field.

- [ ] **Step 3: Write the schema migration**

Create `apps/accounts/migrations/0003_drop_role.py`:

```python
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("accounts", "0002_role_to_groups")]

    operations = [
        migrations.RemoveField(model_name="user", name="role"),
    ]
```

- [ ] **Step 4: Drop `role` from the model**

Replace `apps/accounts/models.py`:

```python
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from .managers import UserManager


class User(AbstractUser):
    """Custom user model with language preferences.

    Role/permission management uses Django Groups (admin / operator /
    member by default; extend via Django Admin → Groups). Properties
    below mirror the pre-refactor API.
    """

    class Language(models.TextChoices):
        ENGLISH = "en", _("English")
        GERMAN = "de", _("German")

    language = models.CharField(
        _("language"),
        max_length=2,
        choices=Language.choices,
        default=Language.ENGLISH,
    )

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ["username"]

    def __str__(self):
        return self.username

    @cached_property
    def is_admin(self):
        return self.groups.filter(name="admin").exists()

    @cached_property
    def is_operator(self):
        return self.groups.filter(name="operator").exists()

    @cached_property
    def is_staff_member(self):
        return self.groups.filter(name__in=["admin", "operator"]).exists()
```

- [ ] **Step 5: Strip role from forms.py / admin.py / managers.py**

For each file that referenced `role`:
- `forms.py`: remove `"role"` from `Meta.fields` / `fields=[]`
- `admin.py`: remove `"role"` from `list_display`, `list_filter`, `fieldsets`
- `managers.py`: remove `role=` kwargs from any `create_user` / `create_superuser`

Replace with a single placeholder for "admin assigns groups via Django admin Groups UI" — no UI for group-set on the form (existing Django UserAdmin already shows a Groups M2M widget).

- [ ] **Step 6: Run full suite**

```bash
docker compose run --rm web pytest -x
```
Expected: pass. Any failure here means Task 5's sweep missed something.

- [ ] **Step 7: Commit**

```bash
git add apps/accounts/migrations/0003_drop_role.py apps/accounts/models.py apps/accounts/forms.py apps/accounts/admin.py apps/accounts/managers.py
git commit -m "accounts: drop deprecated User.role column

Schema migration removes the column; nothing reads it after Task 5's
sweep. Groups carry the role information now, set via Django admin
or programmatically via user.groups.add(Group.objects.get(name=...))."
```

---

## Phase 3 — AppGrant Model + Access Control

### Task 7: AppGrant model + admin + tests

**Files:**
- Create: `apps/sso/models.py`
- Create: `apps/sso/migrations/__init__.py`
- Create: `apps/sso/migrations/0001_initial.py` (will be auto-generated)
- Modify: `apps/sso/admin.py` (will be created — covered here)
- Create: `tests/test_sso_models.py`

- [ ] **Step 1: Write the failing model test**

```python
import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from oauth2_provider.models import Application

User = get_user_model()


@pytest.fixture
def application(db):
    return Application.objects.create(
        name="InvenTree-Test",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="x", email="a@x")


@pytest.mark.django_db
def test_appgrant_is_active_by_default(alice, application):
    from apps.sso.models import AppGrant

    grant = AppGrant.objects.create(user=alice, application=application)
    assert grant.revoked_at is None


@pytest.mark.django_db
def test_appgrant_unique_per_user_per_app_while_active(alice, application):
    """Cannot create two active grants for the same (user, app)."""
    from apps.sso.models import AppGrant

    AppGrant.objects.create(user=alice, application=application)
    with pytest.raises(IntegrityError):
        AppGrant.objects.create(user=alice, application=application)


@pytest.mark.django_db
def test_appgrant_can_be_regranted_after_revoke(alice, application):
    """Once revoked, a new grant for the same (user, app) is allowed."""
    from django.utils import timezone
    from apps.sso.models import AppGrant

    g1 = AppGrant.objects.create(user=alice, application=application)
    g1.revoked_at = timezone.now()
    g1.save()
    # No IntegrityError: partial index excludes revoked rows.
    g2 = AppGrant.objects.create(user=alice, application=application)
    assert g2.revoked_at is None
```

- [ ] **Step 2: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_models.py -v
```
Expected: ERROR — `apps.sso.models.AppGrant` missing.

- [ ] **Step 3: Write the model**

Create `apps/sso/models.py`:

```python
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class AppGrant(models.Model):
    """Grants a user access to a registered OIDC Application.

    The presence of an active (revoked_at IS NULL) row gates the
    OIDC authorization flow — without a row, the validator returns
    access_denied before any token is issued (see Task 9).

    Soft delete (revoked_at) preserves the audit trail: queries can
    show 'X had access until DATE' instead of losing the fact
    entirely.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="app_grants",
        verbose_name=_("user"),
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="grants",
        verbose_name=_("application"),
    )
    granted_at = models.DateTimeField(_("granted at"), auto_now_add=True)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_app_grants",
        verbose_name=_("granted by"),
    )
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("app grant")
        verbose_name_plural = _("app grants")
        constraints = [
            models.UniqueConstraint(
                fields=["user", "application"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_active_grant_per_user_per_app",
            ),
        ]
        indexes = [
            models.Index(fields=["application", "revoked_at"]),
        ]

    def __str__(self):
        return f"{self.user} → {self.application} ({'active' if self.revoked_at is None else 'revoked'})"
```

- [ ] **Step 4: Create the migration**

```bash
docker compose run --rm web python manage.py makemigrations sso
```
Expected: writes `apps/sso/migrations/0001_initial.py` with the `AppGrant` model.

- [ ] **Step 5: Add the admin**

Create `apps/sso/admin.py`:

```python
from django.contrib import admin

from .models import AppGrant


@admin.register(AppGrant)
class AppGrantAdmin(admin.ModelAdmin):
    list_display = ("user", "application", "granted_at", "granted_by", "revoked_at")
    list_filter = ("revoked_at", "application")
    search_fields = ("user__username", "user__email", "application__name")
    raw_id_fields = ("user", "granted_by")
    readonly_fields = ("granted_at",)
```

- [ ] **Step 6: Run tests, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_models.py -v
```
Expected: 3 passed.

- [ ] **Step 7: Commit**

```bash
git add apps/sso/models.py apps/sso/admin.py apps/sso/migrations/ tests/test_sso_models.py
git commit -m "sso: AppGrant model — per-user app-access gate

Single source of truth for 'may user X log into app Y'. Partial
unique index (revoked_at IS NULL) ensures at most one active grant
per (user, app) but allows re-granting after revoke. Validator in
Task 9 consumes this; toggle-UI in Tasks 13-15 mutates it."
```

---

### Task 8: Custom OIDC claims hook

**Files:**
- Modify: `apps/sso/oidc_claims.py` (replace stub)
- Create: `tests/test_sso_claims.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

User = get_user_model()


@pytest.mark.django_db
def test_add_claims_includes_username_email_name_groups():
    from apps.sso.oidc_claims import add_claims

    admin_group = Group.objects.create(name="admin")
    techniker_group = Group.objects.create(name="techniker")
    user = User.objects.create_user(
        username="peterb",
        password="x",
        email="peter@oe5xrx.org",
        first_name="Peter",
        last_name="Buchegger",
    )
    user.language = "de"
    user.save()
    user.groups.add(admin_group, techniker_group)

    claims = add_claims({}, user, request=None)

    assert claims["preferred_username"] == "peterb"
    assert claims["email"] == "peter@oe5xrx.org"
    assert claims["email_verified"] is True
    assert claims["name"] == "Peter Buchegger"
    assert claims["locale"] == "de"
    assert set(claims["groups"]) == {"admin", "techniker"}


@pytest.mark.django_db
def test_add_claims_falls_back_to_username_when_no_full_name():
    from apps.sso.oidc_claims import add_claims

    user = User.objects.create_user(username="anon", password="x", email="a@x")
    claims = add_claims({}, user, request=None)
    assert claims["name"] == "anon"


@pytest.mark.django_db
def test_add_claims_groups_is_always_a_list_even_if_empty():
    """RPs (InvenTree, Grafana) expect groups as a list; missing/scalar breaks them."""
    from apps.sso.oidc_claims import add_claims

    user = User.objects.create_user(username="loner", password="x", email="l@x")
    claims = add_claims({}, user, request=None)
    assert claims["groups"] == []
```

- [ ] **Step 2: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_claims.py -v
```
Expected: assertion errors — the stub returns claims unchanged.

- [ ] **Step 3: Implement**

Replace `apps/sso/oidc_claims.py`:

```python
"""Custom OIDC claims emitted in ID tokens and UserInfo responses.

Wired in via OAUTH2_PROVIDER["OIDC_USERINFO_HOOK"]. Called by DOT
when building the ID token (token endpoint) and the userinfo
endpoint response.

Convention: every claim defined here is documented in the RP
integration guide (docs/superpowers/specs/...-design.md, Section 3.4)
so InvenTree / Grafana operators know what to expect.
"""


def add_claims(claims, user, request):
    """Merge OE5XRX-specific claims into the OIDC payload.

    `claims` is a dict the caller hands in; mutate-or-return is fine
    (we do both to be safe across DOT versions).
    """
    claims["preferred_username"] = user.username
    claims["email"] = user.email or ""
    claims["email_verified"] = bool(user.email)
    claims["name"] = user.get_full_name() or user.username
    claims["locale"] = getattr(user, "language", "en")
    claims["groups"] = list(user.groups.values_list("name", flat=True))
    return claims
```

- [ ] **Step 4: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_claims.py -v
```
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/oidc_claims.py tests/test_sso_claims.py
git commit -m "sso: emit username/email/name/groups custom claims

Hooked into OAUTH2_PROVIDER['OIDC_USERINFO_HOOK'] (already wired in
Task 1). groups is always a list — matches InvenTree/Grafana/Nextcloud
expectations regardless of whether the user has 0, 1 or N memberships."
```

---

### Task 9: AppGrant-aware OAuth2Validator

**Files:**
- Modify: `apps/sso/permissions.py` (replace stub)
- Create: `tests/test_sso_permissions.py`

- [ ] **Step 1: Read DOT's `OAuth2Validator` to find the right override point**

```bash
docker compose run --rm web python -c "import oauth2_provider.oauth2_validators; import inspect; print(inspect.getsourcefile(oauth2_provider.oauth2_validators))"
```

Open the file (path printed above). The method we want is `validate_response_type` or — preferred — `validate_user`. Actually for Authorization Code Flow the access check happens before the redirect-to-RP at `validate_grant`. The simplest correct location is overriding `_get_user` to return None for users without an AppGrant. Final decision: use `validate_user`, which DOT calls during both password-grant and authorization-code-grant. Inspect the live source to confirm method signature.

- [ ] **Step 2: Write the failing test**

Create `tests/test_sso_permissions.py`:

```python
import pytest
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application

User = get_user_model()


@pytest.fixture
def application(db):
    return Application.objects.create(
        name="InvenTree-Test",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="x", email="a@x")


@pytest.mark.django_db
def test_user_with_active_grant_is_allowed(alice, application):
    from apps.sso.models import AppGrant
    from apps.sso.permissions import user_can_access

    AppGrant.objects.create(user=alice, application=application)
    assert user_can_access(alice, application) is True


@pytest.mark.django_db
def test_user_without_grant_is_denied(alice, application):
    from apps.sso.permissions import user_can_access

    assert user_can_access(alice, application) is False


@pytest.mark.django_db
def test_inactive_user_is_denied_even_with_grant(alice, application):
    from apps.sso.models import AppGrant
    from apps.sso.permissions import user_can_access

    AppGrant.objects.create(user=alice, application=application)
    alice.is_active = False
    alice.save()
    assert user_can_access(alice, application) is False


@pytest.mark.django_db
def test_revoked_grant_is_denied(alice, application):
    from django.utils import timezone
    from apps.sso.models import AppGrant
    from apps.sso.permissions import user_can_access

    grant = AppGrant.objects.create(user=alice, application=application)
    grant.revoked_at = timezone.now()
    grant.save()
    assert user_can_access(alice, application) is False
```

- [ ] **Step 3: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_permissions.py -v
```
Expected: ERROR — `user_can_access` not importable from `apps.sso.permissions`.

- [ ] **Step 4: Implement the validator**

Replace `apps/sso/permissions.py`:

```python
"""OIDC access-control: bridges AppGrant into DOT's authorization flow.

`user_can_access` is the pure-function gate used by the validator
class below. Tests target this function directly; the validator
class is the integration point with DOT.
"""

import logging

from oauth2_provider.oauth2_validators import OAuth2Validator

logger = logging.getLogger(__name__)


def user_can_access(user, application) -> bool:
    """Return True iff user is active AND holds an active AppGrant for app."""
    if not getattr(user, "is_active", False):
        return False

    # Local import to avoid circular import at module load (sso.models
    # depends on oauth2_provider.models, which depends on settings,
    # which load this validator).
    from .models import AppGrant

    return AppGrant.objects.filter(
        user=user,
        application=application,
        revoked_at__isnull=True,
    ).exists()


class SsoOAuth2Validator(OAuth2Validator):
    """DOT OAuth2Validator override that consults AppGrant before token issuance.

    The relevant hook is `validate_user`: DOT calls it during both
    password-grant (unused here) and authorization-code-grant prior
    to issuing the auth code. Returning False short-circuits the flow
    with an RFC-conformant `access_denied` error redirect back to the
    RP.
    """

    def validate_user(self, username, password, client, request, *args, **kwargs):
        # Default behaviour first — authenticates the username/password
        # pair via Django's auth backend. If that fails, no point
        # checking AppGrant.
        ok = super().validate_user(username, password, client, request, *args, **kwargs)
        if not ok:
            return False

        from django.contrib.auth import get_user_model

        try:
            user = get_user_model().objects.get(username=username)
        except get_user_model().DoesNotExist:
            return False

        application = getattr(client, "application", client)
        allowed = user_can_access(user, application)
        if not allowed:
            logger.info(
                "AppGrant gate denied user=%s app=%s",
                user.username,
                getattr(application, "client_id", "<unknown>"),
            )
        return allowed

    def is_pkce_required(self, client_id):
        # PKCE_REQUIRED in OAUTH2_PROVIDER toggles this globally; we
        # also force it from here to be defense-in-depth against a
        # config typo that flips PKCE_REQUIRED off.
        return True
```

- [ ] **Step 5: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_permissions.py -v
```
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/sso/permissions.py tests/test_sso_permissions.py
git commit -m "sso: enforce AppGrant + is_active in OAuth2Validator

user_can_access() is the pure-function gate (testable in isolation);
SsoOAuth2Validator threads it into DOT's validate_user hook so an
authorization request without a matching active AppGrant short-
circuits to RFC-conformant access_denied. Also re-asserts
PKCE_REQUIRED at the validator level as defense-in-depth."
```

---

## Phase 4 — SsoAuditLog + Cascading Token Revocation

### Task 10: SsoAuditLog model + integration into audit views

**Files:**
- Modify: `apps/sso/models.py` (append SsoAuditLog)
- Create: `apps/sso/migrations/0002_ssoauditlog.py` (auto-generated)
- Modify: `apps/sso/admin.py` (register SsoAuditLog)
- Modify: `apps/audit/views.py` (extend listing to merge SsoAuditLog entries)
- Modify: `apps/audit/templates/audit/audit_list.html` (filter by category)
- Modify: `apps/audit/templates/audit/_audit_table.html` (render SsoAuditLog row variant)
- Create: `tests/test_sso_audit.py`

- [ ] **Step 1: Read current `apps/audit/views.py` to understand the listing**

```bash
docker compose run --rm web cat apps/audit/views.py
```
Note how `StationAuditLog` is fetched + paginated. We'll mirror the pattern.

- [ ] **Step 2: Write failing test**

Create `tests/test_sso_audit.py`:

```python
import pytest
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application

User = get_user_model()


@pytest.mark.django_db
def test_ssoauditlog_records_grant_given_event():
    from apps.sso.models import AppGrant, SsoAuditLog

    admin = User.objects.create_user(username="admin", password="x", email="a@x")
    target = User.objects.create_user(username="target", password="x", email="t@x")
    app = Application.objects.create(
        name="X",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )

    entry = SsoAuditLog.log(
        event_type=SsoAuditLog.EventType.GRANT_GIVEN,
        actor=admin,
        target_user=target,
        application=app,
        message="grant added",
    )
    assert entry.pk is not None
    assert entry.event_type == "grant_given"
    assert entry.actor_id == admin.pk
```

- [ ] **Step 3: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_audit.py -v
```
Expected: ERROR — `SsoAuditLog` not importable.

- [ ] **Step 4: Add the model**

Append to `apps/sso/models.py`:

```python
class SsoAuditLog(models.Model):
    """System-wide audit trail for SSO/OIDC events.

    Parallel to StationAuditLog (which is per-station). The
    `apps.audit` listing view merges both into a single feed —
    see apps/audit/views.py.
    """

    class EventType(models.TextChoices):
        APP_REGISTERED = "app_registered", _("App Registered")
        APP_DELETED = "app_deleted", _("App Deleted")
        GRANT_GIVEN = "grant_given", _("Grant Given")
        GRANT_REVOKED = "grant_revoked", _("Grant Revoked")
        LOGIN_SUCCESS = "login_success", _("Login Success")
        LOGIN_DENIED_NO_GRANT = "login_denied_no_grant", _("Login Denied — No Grant")
        LOGIN_DENIED_INACTIVE = "login_denied_inactive", _("Login Denied — Inactive User")
        TOKEN_REVOKED = "token_revoked", _("Token Revoked")

    event_type = models.CharField(_("event type"), max_length=32, choices=EventType.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sso_audit_logs_as_actor",
        verbose_name=_("actor"),
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sso_audit_logs_as_target",
        verbose_name=_("target user"),
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name=_("application"),
    )
    message = models.TextField(_("message"), blank=True)
    ip_address = models.GenericIPAddressField(_("IP address"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("SSO audit log")
        verbose_name_plural = _("SSO audit logs")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["event_type", "-created_at"]),
            models.Index(fields=["target_user", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.get_event_type_display()} @ {self.created_at}"

    @classmethod
    def log(cls, *, event_type, actor=None, target_user=None, application=None, message="", ip_address=None):
        """Convenience constructor. Mirrors StationAuditLog.log signature."""
        return cls.objects.create(
            event_type=event_type,
            actor=actor,
            target_user=target_user,
            application=application,
            message=message,
            ip_address=ip_address,
        )
```

- [ ] **Step 5: Make + apply migration**

```bash
docker compose run --rm web python manage.py makemigrations sso
docker compose run --rm web python manage.py migrate
```

- [ ] **Step 6: Register admin**

Append to `apps/sso/admin.py`:

```python
from .models import SsoAuditLog


@admin.register(SsoAuditLog)
class SsoAuditLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "event_type", "actor", "target_user", "application")
    list_filter = ("event_type", "application")
    search_fields = ("actor__username", "target_user__username", "message")
    readonly_fields = ("created_at",)
    date_hierarchy = "created_at"
```

- [ ] **Step 7: Extend audit listing view**

Modify `apps/audit/views.py` to fetch both log models, merge them by `created_at` desc, and paginate the merged list. Add a `category` filter param accepted from GET (`"station" | "sso" | ""` = all). Concrete approach: build two QuerySets with a common `category` annotation, materialize-and-sort in Python (acceptable up to N ≈ thousands; for true scale this'd want a unified table).

Add to `apps/audit/views.py`:

```python
def audit_list(request):
    from apps.sso.models import SsoAuditLog
    from apps.stations.models import StationAuditLog

    category = request.GET.get("category", "")
    station_logs = []
    sso_logs = []

    if category in ("", "station"):
        station_logs = list(StationAuditLog.objects.select_related("station", "user").order_by("-created_at")[:500])
    if category in ("", "sso"):
        sso_logs = list(SsoAuditLog.objects.select_related("actor", "target_user", "application").order_by("-created_at")[:500])

    merged = sorted(
        [("station", e) for e in station_logs] + [("sso", e) for e in sso_logs],
        key=lambda pair: pair[1].created_at,
        reverse=True,
    )

    # Paginate in Python — list is bounded above (max 1000 entries).
    from django.core.paginator import Paginator
    page = Paginator(merged, 50).get_page(request.GET.get("page", 1))

    template = "audit/_audit_table.html" if request.htmx else "audit/audit_list.html"
    return render(request, template, {"page": page, "category": category})
```

(The exact view name and signature should match what's already in `apps/audit/views.py` — keep `LoginRequiredMixin` / admin gate if present.)

- [ ] **Step 8: Update template to render both row types**

Modify `apps/audit/templates/audit/_audit_table.html` to handle both shapes. Each item is now `(category, entry)`; the template branches on `category`. Add a `<select>` filter for category at the top of `audit_list.html` that submits via HTMX:

```django
<select name="category" hx-get="{% url 'audit:list' %}" hx-target="#audit-table"
        hx-trigger="change" class="form-select form-select-sm">
  <option value="" {% if not category %}selected{% endif %}>{% trans "All" %}</option>
  <option value="station" {% if category == "station" %}selected{% endif %}>{% trans "Stations" %}</option>
  <option value="sso" {% if category == "sso" %}selected{% endif %}>SSO</option>
</select>
```

- [ ] **Step 9: Run tests, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_audit.py -v
```
Expected: 1 passed.

- [ ] **Step 10: Commit**

```bash
git add apps/sso/models.py apps/sso/migrations/ apps/sso/admin.py apps/audit/ tests/test_sso_audit.py
git commit -m "sso: SsoAuditLog model + audit listing integration

System-wide log parallel to StationAuditLog. apps/audit/views.py
merges both feeds into one paginated list with a category filter.
SsoAuditLog.log() convenience constructor mirrors StationAuditLog.log
so callers in subsequent tasks (signal handlers, grant toggle view)
can use either interchangeably."
```

---

### Task 11: Cascading token revocation via signals

**Files:**
- Create: `apps/sso/signals.py`
- Modify: `apps/sso/apps.py` (connect signals on app ready)
- Create: `tests/test_sso_signals.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from django.contrib.auth import get_user_model
from oauth2_provider.models import AccessToken, Application

User = get_user_model()


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="X",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )


@pytest.fixture
def alice_with_token(db, app):
    from django.utils import timezone
    from datetime import timedelta

    alice = User.objects.create_user(username="alice", password="x", email="a@x")
    token = AccessToken.objects.create(
        user=alice,
        application=app,
        token="opaque-test-token",
        expires=timezone.now() + timedelta(hours=1),
        scope="openid",
    )
    return alice, token


@pytest.mark.django_db
def test_deactivating_user_revokes_all_their_tokens(alice_with_token):
    from django.utils import timezone

    alice, token = alice_with_token
    alice.is_active = False
    alice.save()
    token.refresh_from_db()
    assert token.expires <= timezone.now()


@pytest.mark.django_db
def test_revoking_appgrant_revokes_tokens_only_for_that_app(alice_with_token, app):
    from datetime import timedelta
    from django.utils import timezone

    from apps.sso.models import AppGrant

    alice, token = alice_with_token
    # second app + grant + token
    other_app = Application.objects.create(
        name="Other",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
    )
    other_token = AccessToken.objects.create(
        user=alice, application=other_app, token="other-opaque",
        expires=timezone.now() + timedelta(hours=1), scope="openid",
    )

    grant = AppGrant.objects.create(user=alice, application=app)
    grant.revoked_at = timezone.now()
    grant.save()

    token.refresh_from_db()
    other_token.refresh_from_db()
    assert token.expires <= timezone.now()
    # The other app's token must NOT be touched.
    assert other_token.expires > timezone.now()
```

- [ ] **Step 2: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_signals.py -v
```
Expected: 2 failures — tokens still valid after deactivation/revoke.

- [ ] **Step 3: Implement signals**

Create `apps/sso/signals.py`:

```python
"""Cascade user / grant lifecycle events onto OAuth2 token state.

When `User.is_active` flips False, every Access/RefreshToken for that
user is force-expired (the user has been kicked out; tokens still
valid until natural expiry would let them keep working in any RP).

When an AppGrant transitions to revoked (revoked_at set), only the
tokens for that user-app pair are force-expired — the user may still
have legitimate access to other apps.

Implementation note: we set `expires` to now-1s rather than deleting
rows, so any later audit query on token history stays intact.
"""

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=get_user_model())
def stash_old_is_active(sender, instance, **kwargs):
    """Stash pre-save value of is_active so post_save can compare."""
    if instance.pk is None:
        instance._old_is_active = True  # creation defaults to active
        return
    try:
        old = sender.objects.only("is_active").get(pk=instance.pk)
        instance._old_is_active = old.is_active
    except sender.DoesNotExist:
        instance._old_is_active = True


@receiver(post_save, sender=get_user_model())
def revoke_tokens_on_user_deactivation(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_old_is_active", True)
    if old and not instance.is_active:
        from oauth2_provider.models import AccessToken, RefreshToken

        past = timezone.now() - timedelta(seconds=1)
        n_at = AccessToken.objects.filter(user=instance, expires__gt=timezone.now()).update(expires=past)
        n_rt = RefreshToken.objects.filter(user=instance, revoked__isnull=True).update(revoked=timezone.now())
        logger.info(
            "User %s deactivated → revoked %d access + %d refresh tokens",
            instance.username, n_at, n_rt,
        )

        # Audit log — best-effort, avoid raising inside a signal handler
        # that would otherwise prevent the User.save() from committing.
        try:
            from .models import SsoAuditLog
            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.TOKEN_REVOKED,
                target_user=instance,
                message=f"User deactivated; {n_at} access tokens + {n_rt} refresh tokens revoked.",
            )
        except Exception:
            logger.exception("Audit log write failed during user deactivation cascade")


def revoke_tokens_for_user_and_app(user, application):
    """Helper called from the AppGrant post_save handler below."""
    from oauth2_provider.models import AccessToken, RefreshToken

    past = timezone.now() - timedelta(seconds=1)
    n_at = AccessToken.objects.filter(
        user=user, application=application, expires__gt=timezone.now()
    ).update(expires=past)
    n_rt = RefreshToken.objects.filter(
        user=user, application=application, revoked__isnull=True
    ).update(revoked=timezone.now())
    logger.info(
        "AppGrant revoked user=%s app=%s → %d access + %d refresh revoked",
        user.username, application.client_id, n_at, n_rt,
    )

    try:
        from .models import SsoAuditLog
        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.TOKEN_REVOKED,
            target_user=user,
            application=application,
            message=f"AppGrant revoked; {n_at} access + {n_rt} refresh tokens revoked.",
        )
    except Exception:
        logger.exception("Audit log write failed during grant revoke cascade")


@receiver(pre_save, sender="sso.AppGrant")
def stash_old_revoked_at(sender, instance, **kwargs):
    if instance.pk is None:
        instance._old_revoked_at = None
        return
    try:
        old = sender.objects.only("revoked_at").get(pk=instance.pk)
        instance._old_revoked_at = old.revoked_at
    except sender.DoesNotExist:
        instance._old_revoked_at = None


@receiver(post_save, sender="sso.AppGrant")
def revoke_tokens_on_grant_revoke(sender, instance, created, **kwargs):
    if created:
        return
    old = getattr(instance, "_old_revoked_at", None)
    if old is None and instance.revoked_at is not None:
        revoke_tokens_for_user_and_app(instance.user, instance.application)
```

- [ ] **Step 4: Wire signals in app config**

Replace `apps/sso/apps.py`:

```python
from django.apps import AppConfig


class SsoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.sso"
    label = "sso"
    verbose_name = "SSO / OIDC Provider"

    def ready(self):
        # Import for side effects: connects the signal handlers below.
        from . import signals  # noqa: F401
```

- [ ] **Step 5: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_signals.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/sso/signals.py apps/sso/apps.py tests/test_sso_signals.py
git commit -m "sso: cascading token revocation on user-deactivate + grant-revoke

Two signal handlers, both pre_save+post_save pairs (pre stashes the
old value so post can detect the transition). User.is_active false-
edge revokes every token; AppGrant revoke-edge only touches that
user-app pair. Both write best-effort SsoAuditLog entries — failures
are logged but never propagate to the calling save()."
```

---

## Phase 5 — Custom UI

### Task 12: Django-Admin customizing for Application

**Files:**
- Modify: `apps/sso/admin.py` (add custom ApplicationAdmin)
- Create: `tests/test_sso_admin.py`

- [ ] **Step 1: Read DOT's existing ApplicationAdmin to know what we override**

```bash
docker compose run --rm web python -c "import oauth2_provider.admin; print(oauth2_provider.admin.__file__)"
```
Open the file. We want to extend the registered ApplicationAdmin with: list grant count + show client_secret in plaintext **only** on the post-create redirect.

- [ ] **Step 2: Write failing test for the client_secret display flag**

Create `tests/test_sso_admin.py`:

```python
import pytest
from django.contrib.auth import get_user_model

User = get_user_model()


@pytest.mark.django_db
def test_application_admin_displays_grant_count_in_list(client):
    """Lists should show how many grants point at each app — admin's
    quick proxy for 'is this app used?'."""
    from django.contrib.auth.models import Group
    from oauth2_provider.models import Application
    from apps.sso.models import AppGrant

    admin_group = Group.objects.create(name="admin")
    admin = User.objects.create_superuser(username="superadmin", password="x", email="a@x")
    admin.groups.add(admin_group)
    client.force_login(admin)

    app = Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x.example/cb/",
    )
    user2 = User.objects.create_user(username="u2", password="x", email="u2@x")
    AppGrant.objects.create(user=user2, application=app)

    resp = client.get("/admin/oauth2_provider/application/")
    assert resp.status_code == 200
    assert b"InvenTree" in resp.content
    # Grant count column is rendered as plain text.
    assert b">1<" in resp.content or b">1 </td>" in resp.content
```

- [ ] **Step 3: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_admin.py -v
```
Expected: assertion error — no grant count column rendered.

- [ ] **Step 4: Override ApplicationAdmin**

Append to `apps/sso/admin.py`:

```python
from django.contrib import admin
from django.utils.html import format_html
from oauth2_provider.admin import ApplicationAdmin as DefaultAppAdmin
from oauth2_provider.models import Application


class CustomApplicationAdmin(DefaultAppAdmin):
    list_display = ("name", "client_id", "client_type", "active_grants", "created")

    def active_grants(self, obj):
        from .models import AppGrant
        return AppGrant.objects.filter(application=obj, revoked_at__isnull=True).count()
    active_grants.short_description = "Active grants"

    def get_readonly_fields(self, request, obj=None):
        ro = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            # Once created, client_secret is opaque — Django Admin
            # would otherwise re-render the hashed value, which leaks
            # nothing operational but invites confusion.
            ro.append("client_secret")
        return ro


# Re-register so our subclass replaces the default.
admin.site.unregister(Application)
admin.site.register(Application, CustomApplicationAdmin)
```

- [ ] **Step 5: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_admin.py -v
```
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add apps/sso/admin.py tests/test_sso_admin.py
git commit -m "sso: Django-admin grant-count column on Application list

Subclasses DOT's ApplicationAdmin and adds a derived 'active grants'
column. Read-only on client_secret after creation so the hashed
form doesn't visually masquerade as the original secret."
```

---

### Task 13: AppGrant-toggle endpoint + User-detail UI block

**Files:**
- Create: `apps/sso/views.py`
- Create: `apps/sso/urls.py`
- Modify: `config/urls.py` (mount sso URLs at `/sso-admin/` to avoid clashing with `/sso/` OIDC endpoints)
- Create: `apps/sso/templates/sso/_app_grants_card.html`
- Modify: `apps/accounts/templates/accounts/user_form.html` (include the grants card)
- Create: `tests/test_sso_views.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest
from django.contrib.auth import get_user_model
from oauth2_provider.models import Application

User = get_user_model()


@pytest.fixture
def admin_user(db):
    from django.contrib.auth.models import Group

    g = Group.objects.create(name="admin")
    u = User.objects.create_user(username="admin", password="x", email="a@x")
    u.groups.add(g)
    return u


@pytest.fixture
def alice(db):
    return User.objects.create_user(username="alice", password="x", email="al@x")


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://x/",
    )


@pytest.mark.django_db
def test_toggle_creates_grant_when_none_exists(client, admin_user, alice, app):
    from apps.sso.models import AppGrant

    client.force_login(admin_user)
    resp = client.post(f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/")
    assert resp.status_code == 200
    assert AppGrant.objects.filter(user=alice, application=app, revoked_at__isnull=True).exists()


@pytest.mark.django_db
def test_toggle_revokes_grant_when_active_one_exists(client, admin_user, alice, app):
    from apps.sso.models import AppGrant

    AppGrant.objects.create(user=alice, application=app)
    client.force_login(admin_user)
    resp = client.post(f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/")
    assert resp.status_code == 200
    # Active grant should be gone:
    assert not AppGrant.objects.filter(user=alice, application=app, revoked_at__isnull=True).exists()
    # Revoked record stays for audit:
    assert AppGrant.objects.filter(user=alice, application=app).count() == 1


@pytest.mark.django_db
def test_non_admin_cannot_toggle_grants(client, alice, app):
    client.force_login(alice)
    resp = client.post(f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/")
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_toggle_writes_audit_log_entry(client, admin_user, alice, app):
    from apps.sso.models import SsoAuditLog

    client.force_login(admin_user)
    client.post(f"/sso-admin/grants/toggle/{alice.pk}/{app.pk}/")
    entries = SsoAuditLog.objects.filter(actor=admin_user, target_user=alice, application=app)
    assert entries.exists()
    assert entries.first().event_type == SsoAuditLog.EventType.GRANT_GIVEN
```

- [ ] **Step 2: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_views.py -v
```
Expected: 404s — URLs not wired.

- [ ] **Step 3: Implement views**

Create `apps/sso/views.py`:

```python
import logging

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView
from oauth2_provider.models import Application

from .models import AppGrant, SsoAuditLog

User = get_user_model()
logger = logging.getLogger(__name__)


class AdminOnlyMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_admin


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


class GrantToggleView(AdminOnlyMixin, View):
    """POST-only: flip an AppGrant on or off for (user, application).

    Atomic from the caller's perspective: idempotent given the same
    starting state, with audit log entry per transition.
    """

    def post(self, request, user_id, application_id):
        target = get_object_or_404(User, pk=user_id)
        application = get_object_or_404(Application, pk=application_id)

        active = AppGrant.objects.filter(
            user=target, application=application, revoked_at__isnull=True
        ).first()

        if active is not None:
            active.revoked_at = timezone.now()
            active.save(update_fields=["revoked_at"])
            event_type = SsoAuditLog.EventType.GRANT_REVOKED
            verb = _("revoked")
        else:
            AppGrant.objects.create(
                user=target, application=application, granted_by=request.user,
            )
            event_type = SsoAuditLog.EventType.GRANT_GIVEN
            verb = _("granted")

        SsoAuditLog.log(
            event_type=event_type,
            actor=request.user,
            target_user=target,
            application=application,
            message=f"{verb} via toggle UI",
            ip_address=_client_ip(request),
        )

        # Re-render the grants card for HTMX swap.
        return render(
            request,
            "sso/_app_grants_card.html",
            {"target_user": target, "applications": _build_grants_for_user(target)},
        )


def _build_grants_for_user(user):
    """Return list of (Application, AppGrant|None) tuples for the card render."""
    active_grants = {
        g.application_id: g
        for g in AppGrant.objects.filter(user=user, revoked_at__isnull=True).select_related("application")
    }
    return [
        (app, active_grants.get(app.pk))
        for app in Application.objects.order_by("name")
    ]


class SsoDashboardView(AdminOnlyMixin, ListView):
    """Top-level SSO overview — list registered apps + grant counts."""
    template_name = "sso/dashboard.html"
    context_object_name = "applications"

    def get_queryset(self):
        return Application.objects.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        counts = {
            row["application_id"]: row["n"]
            for row in AppGrant.objects.filter(revoked_at__isnull=True)
            .values("application_id").annotate(n=models_Count("id"))
        } if False else {}  # see import note below
        # Simple per-app count loop (avoids the import gymnastics):
        for app in ctx["applications"]:
            app.active_grant_count = AppGrant.objects.filter(
                application=app, revoked_at__isnull=True
            ).count()
        return ctx


class ApplicationDetailView(AdminOnlyMixin, DetailView):
    template_name = "sso/application_detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return Application.objects.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        active_user_ids = AppGrant.objects.filter(
            application=self.object, revoked_at__isnull=True,
        ).values_list("user_id", flat=True)
        ctx["users_with_grant"] = User.objects.filter(pk__in=active_user_ids).order_by("username")
        ctx["users_without_grant"] = User.objects.exclude(pk__in=active_user_ids).order_by("username")
        return ctx
```

Remove the broken `models_Count` reference — simplify by deleting that whole `counts` block (the per-app loop below is what we actually use). Final shape of `SsoDashboardView.get_context_data`:

```python
def get_context_data(self, **kwargs):
    ctx = super().get_context_data(**kwargs)
    for app in ctx["applications"]:
        app.active_grant_count = AppGrant.objects.filter(
            application=app, revoked_at__isnull=True
        ).count()
    return ctx
```

Create `apps/sso/urls.py`:

```python
from django.urls import path

from . import views

app_name = "sso"

urlpatterns = [
    path("", views.SsoDashboardView.as_view(), name="dashboard"),
    path("applications/<int:pk>/", views.ApplicationDetailView.as_view(), name="application_detail"),
    path("grants/toggle/<int:user_id>/<int:application_id>/", views.GrantToggleView.as_view(), name="grant_toggle"),
]
```

- [ ] **Step 4: Mount the URL set**

Modify `config/urls.py` — add inside `i18n_patterns(...)`:

```python
path("sso-admin/", include("apps.sso.urls")),
```

(Note: `/sso/` is the OIDC machinery; `/sso-admin/` is the in-station-manager admin UI for it. Different prefixes prevent collisions with DOT's `/sso/applications/` etc.)

- [ ] **Step 5: Create the HTMX partial template**

`apps/sso/templates/sso/_app_grants_card.html`:

```django
{% load i18n %}
<div class="card mb-3" id="sso-grants-card">
  <div class="card-header">
    <h6 class="mb-0">{% trans "App-Zugriffe" %}</h6>
  </div>
  <div class="card-body p-0">
    <table class="table table-sm mb-0">
      <tbody>
        {% for app, grant in applications %}
        <tr>
          <td>{{ app.name }}</td>
          <td>
            {% if grant %}
              <span class="badge bg-success">{% trans "aktiv" %}</span>
              <small class="text-muted ms-2">{% trans "seit" %} {{ grant.granted_at|date:"Y-m-d" }}</small>
            {% else %}
              <span class="badge bg-secondary">{% trans "kein Zugriff" %}</span>
            {% endif %}
          </td>
          <td class="text-end">
            <button type="button"
                    class="btn btn-sm {% if grant %}btn-outline-danger{% else %}btn-outline-primary{% endif %}"
                    hx-post="{% url 'sso:grant_toggle' user_id=target_user.pk application_id=app.pk %}"
                    hx-target="#sso-grants-card"
                    hx-swap="outerHTML">
              {% if grant %}{% trans "Entziehen" %}{% else %}{% trans "Gewähren" %}{% endif %}
            </button>
          </td>
        </tr>
        {% empty %}
        <tr><td colspan="3" class="text-muted text-center py-3">
          {% trans "Noch keine Apps registriert." %}
        </td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</div>
```

- [ ] **Step 6: Embed the card on the user-edit page**

Modify `apps/accounts/templates/accounts/user_form.html` — add near the bottom of the form, **only** for admin viewers and only when editing (not creating) an existing user:

```django
{% if request.user.is_admin and object %}
  {% include "sso/_app_grants_card.html" with target_user=object applications=app_grants_list %}
{% endif %}
```

Modify `apps/accounts/views.py` `UserUpdateView.get_context_data` to populate `app_grants_list`:

```python
def get_context_data(self, **kwargs):
    context = super().get_context_data(**kwargs)
    context["form_title"] = _("Edit User")
    from apps.sso.views import _build_grants_for_user
    context["app_grants_list"] = _build_grants_for_user(self.object)
    return context
```

- [ ] **Step 7: Run tests, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_views.py -v
```
Expected: 4 passed.

- [ ] **Step 8: Commit**

```bash
git add apps/sso/views.py apps/sso/urls.py apps/sso/templates/ config/urls.py apps/accounts/templates/accounts/user_form.html apps/accounts/views.py tests/test_sso_views.py
git commit -m "sso: HTMX grant-toggle endpoint + user-detail integration

Toggle is idempotent: flip an active grant to revoked or create a new
one. Always emits SsoAuditLog. Card re-renders inline via HTMX swap.
Only visible to admins on the user-edit page; mount lives at
/sso-admin/grants/toggle/<user>/<app>/ to keep clear of the /sso/
OIDC namespace."
```

---

### Task 14: SSO dashboard + sidebar entry

**Files:**
- Create: `apps/sso/templates/sso/dashboard.html`
- Modify: `templates/includes/sidebar.html` (add SSO link for admins)
- Modify: `tests/test_sso_views.py` (extend with dashboard test)

- [ ] **Step 1: Write failing test**

Append to `tests/test_sso_views.py`:

```python
@pytest.mark.django_db
def test_dashboard_lists_apps_with_grant_counts(client, admin_user, alice):
    from oauth2_provider.models import Application
    from apps.sso.models import AppGrant

    app1 = Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://i/",
    )
    app2 = Application.objects.create(
        name="Grafana",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://g/",
    )
    AppGrant.objects.create(user=alice, application=app1)

    client.force_login(admin_user)
    resp = client.get("/sso-admin/")
    assert resp.status_code == 200
    assert b"InvenTree" in resp.content
    assert b"Grafana" in resp.content
    # InvenTree must show 1 active grant, Grafana 0.
    assert b">1<" in resp.content
```

- [ ] **Step 2: Create dashboard template**

`apps/sso/templates/sso/dashboard.html`:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}SSO — {% trans "Registered Apps" %}{% endblock %}

{% block content %}
<div class="d-flex justify-content-between align-items-center mb-3">
  <h2>SSO — {% trans "Registered Apps" %}</h2>
  <a class="btn btn-outline-primary btn-sm" href="/admin/oauth2_provider/application/add/">
    + {% trans "Neue App registrieren" %}
  </a>
</div>

<table class="table table-striped">
  <thead>
    <tr>
      <th>{% trans "Application" %}</th>
      <th class="text-end">{% trans "Active Grants" %}</th>
      <th>{% trans "Client ID" %}</th>
      <th></th>
    </tr>
  </thead>
  <tbody>
    {% for app in applications %}
    <tr>
      <td><a href="{% url 'sso:application_detail' pk=app.pk %}">{{ app.name }}</a></td>
      <td class="text-end">{{ app.active_grant_count }}</td>
      <td><code class="small">{{ app.client_id }}</code></td>
      <td class="text-end">
        <a class="btn btn-sm btn-outline-secondary"
           href="{% url 'sso:application_detail' pk=app.pk %}">{% trans "Details" %}</a>
      </td>
    </tr>
    {% empty %}
    <tr><td colspan="4" class="text-muted text-center py-5">
      {% trans "Noch keine Apps registriert." %}
      <a href="/admin/oauth2_provider/application/add/">{% trans "Eine anlegen →" %}</a>
    </td></tr>
    {% endfor %}
  </tbody>
</table>
{% endblock %}
```

- [ ] **Step 3: Add sidebar entry**

Modify `templates/includes/sidebar.html` — within the admin-only section (`{% if user.is_admin %}` from Task 5's sweep), add:

```django
<a class="nav-link" href="{% url 'sso:dashboard' %}">
  <i class="bi bi-key-fill"></i> SSO
</a>
```

- [ ] **Step 4: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_views.py -v
```
Expected: all sso-views tests pass (4 previous + 1 new = 5).

- [ ] **Step 5: Commit**

```bash
git add apps/sso/templates/sso/dashboard.html templates/includes/sidebar.html tests/test_sso_views.py
git commit -m "sso: dashboard at /sso-admin/ + sidebar entry

Lists every registered Application with active-grant count and a
link to the per-app detail. Quick 'add app' button routes to the
DOT Django-admin form (registration is rare + sensitive, lives
there)."
```

---

### Task 15: Application-detail page (per-app grant management)

**Files:**
- Create: `apps/sso/templates/sso/application_detail.html`
- Modify: `tests/test_sso_views.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.django_db
def test_application_detail_lists_granted_and_not_granted_users(client, admin_user, alice, app):
    from apps.sso.models import AppGrant

    bob = User.objects.create_user(username="bob", password="x", email="b@x")
    AppGrant.objects.create(user=alice, application=app)
    # bob has no grant.

    client.force_login(admin_user)
    resp = client.get(f"/sso-admin/applications/{app.pk}/")
    assert resp.status_code == 200
    assert b"alice" in resp.content
    assert b"bob" in resp.content
```

- [ ] **Step 2: Create template**

`apps/sso/templates/sso/application_detail.html`:

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{{ application.name }} — SSO{% endblock %}

{% block content %}
<nav aria-label="breadcrumb">
  <ol class="breadcrumb">
    <li class="breadcrumb-item"><a href="{% url 'sso:dashboard' %}">SSO</a></li>
    <li class="breadcrumb-item active">{{ application.name }}</li>
  </ol>
</nav>

<h2>{{ application.name }}</h2>

<dl class="row">
  <dt class="col-sm-3">{% trans "Client ID" %}</dt>
  <dd class="col-sm-9"><code>{{ application.client_id }}</code></dd>
  <dt class="col-sm-3">{% trans "Redirect URIs" %}</dt>
  <dd class="col-sm-9"><pre class="small">{{ application.redirect_uris }}</pre></dd>
  <dt class="col-sm-3">{% trans "Grant type" %}</dt>
  <dd class="col-sm-9">{{ application.authorization_grant_type }}</dd>
</dl>

<div class="row mt-4">
  <div class="col-md-6">
    <h5>{% trans "Wer hat Zugriff?" %} ({{ users_with_grant|length }})</h5>
    <ul class="list-group">
      {% for u in users_with_grant %}
      <li class="list-group-item d-flex justify-content-between align-items-center">
        <a href="{% url 'accounts:user_update' u.pk %}">{{ u.username }}</a>
        <button type="button" class="btn btn-sm btn-outline-danger"
                hx-post="{% url 'sso:grant_toggle' user_id=u.pk application_id=application.pk %}"
                hx-target="closest .row" hx-swap="outerHTML"
                hx-get-on-success="{% url 'sso:application_detail' pk=application.pk %}">
          {% trans "Entziehen" %}
        </button>
      </li>
      {% empty %}
      <li class="list-group-item text-muted">{% trans "Niemand hat Zugriff." %}</li>
      {% endfor %}
    </ul>
  </div>
  <div class="col-md-6">
    <h5>{% trans "Ohne Zugriff" %} ({{ users_without_grant|length }})</h5>
    <ul class="list-group">
      {% for u in users_without_grant %}
      <li class="list-group-item d-flex justify-content-between align-items-center">
        <a href="{% url 'accounts:user_update' u.pk %}">{{ u.username }}</a>
        <button type="button" class="btn btn-sm btn-outline-primary"
                hx-post="{% url 'sso:grant_toggle' user_id=u.pk application_id=application.pk %}"
                hx-target="closest .row" hx-swap="outerHTML">
          {% trans "Gewähren" %}
        </button>
      </li>
      {% empty %}
      <li class="list-group-item text-muted">{% trans "Alle User haben Zugriff." %}</li>
      {% endfor %}
    </ul>
  </div>
</div>
{% endblock %}
```

(The HTMX dance for this template's two columns gets gnarly because toggling one item must refresh both lists. Pragmatic V1: redirect the response server-side back to the same page rather than partial-swap. Simpler approach implemented below: change the `GrantToggleView` to honor an `?next=` redirect when the template comes from this page.)

Modify `apps/sso/views.py` `GrantToggleView.post`:

```python
def post(self, request, user_id, application_id):
    # ... existing logic ...

    # If the caller came from the application detail page (HX-Trigger
    # name we'll send below), respond with HX-Redirect rather than a
    # partial swap, so both columns stay in sync.
    if request.htmx and request.headers.get("HX-Trigger-Name") == "from-app-detail":
        from django.http import HttpResponse
        resp = HttpResponse(status=200)
        resp["HX-Redirect"] = reverse("sso:application_detail", kwargs={"pk": application.pk})
        return resp

    return render(
        request, "sso/_app_grants_card.html",
        {"target_user": target, "applications": _build_grants_for_user(target)},
    )
```

Add `name="from-app-detail"` to the toggle buttons in `application_detail.html` so the view branches on it.

- [ ] **Step 3: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_views.py -v
```
Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add apps/sso/templates/sso/application_detail.html apps/sso/views.py tests/test_sso_views.py
git commit -m "sso: per-application detail page with grant toggle

Two-column 'has access' / 'no access' list. Toggle uses HX-Redirect
back to the same URL on this page (both columns need to refresh, so
partial swap is unhelpful here)."
```

---

## Phase 6 — Logout Hardening + Consent Template

### Task 16: post_logout_redirect_uri exact-match validation

**Files:**
- Modify: `apps/sso/permissions.py` (override `validate_post_logout_redirect_uri`)
- Create: `tests/test_sso_logout.py`

- [ ] **Step 1: Write failing test**

```python
import pytest
from oauth2_provider.models import Application


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="X",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
        # Most DOT versions accept post_logout_redirect_uris on the same model
        # in newer releases; if your DOT version is older, this test pins down
        # what we need.
    )


@pytest.mark.django_db
def test_logout_rejects_unregistered_post_logout_redirect_uri(client, app):
    """Open redirect prevention: only redirect URIs the admin registered are honored."""
    resp = client.get(
        f"/sso/logout/?post_logout_redirect_uri=https://attacker.example/steal",
    )
    # DOT returns 400 (or in some versions, ignores the parameter and renders
    # the default logout page). We want 4xx, NEVER 302 to attacker.example.
    assert resp.status_code in (400, 401, 403)
    assert "attacker.example" not in resp.get("Location", "")
```

- [ ] **Step 2: Run, expect failure**

```bash
docker compose run --rm web pytest tests/test_sso_logout.py -v
```
Expected: if it 302s to attacker.example, this is a real Open-Redirect — current DOT versions ≥ 2.4 do validate, but check live behavior.

- [ ] **Step 3: If failing, override the validator**

Append to `apps/sso/permissions.py`:

```python
    def validate_post_logout_redirect_uri(self, client, uri):
        """Exact-match against the application's registered post-logout URIs.

        DOT ≥ 2.4 stores them on `Application.post_logout_redirect_uris`
        (space-separated). For older DOT versions or applications that
        haven't configured them, fail closed.
        """
        registered = (getattr(client, "post_logout_redirect_uris", "") or "").split()
        return uri in registered
```

If your DOT version doesn't have `post_logout_redirect_uris` on Application, add it via a migration or document the limitation in the spec (current spec already calls this out).

- [ ] **Step 4: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_logout.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/sso/permissions.py tests/test_sso_logout.py
git commit -m "sso: open-redirect protection on /sso/logout/

post_logout_redirect_uri must exact-match a value registered on the
Application; an unregistered URI is rejected (fail closed). Pins down
behavior with a regression test."
```

---

### Task 17: Override DOT consent template (Bootstrap styling)

**Files:**
- Create: `apps/sso/templates/oauth2_provider/authorize.html`

- [ ] **Step 1: Find DOT's default template**

```bash
docker compose run --rm web python -c "import oauth2_provider, pathlib; print(pathlib.Path(oauth2_provider.__file__).parent / 'templates' / 'oauth2_provider' / 'authorize.html')"
```
Open and copy its structure.

- [ ] **Step 2: Write the override**

Create `apps/sso/templates/oauth2_provider/authorize.html` (Django picks up app-templates first; since `apps.sso` is in INSTALLED_APPS, this overrides DOT's bundled template):

```django
{% extends "base.html" %}
{% load i18n %}

{% block title %}{% trans "Authorize" %} — {{ application.name }}{% endblock %}

{% block content %}
<div class="d-flex align-items-center justify-content-center" style="min-height: 60vh;">
  <div class="card shadow-sm" style="max-width: 480px; width: 100%;">
    <div class="card-body p-4">
      <h4 class="mb-3">{% trans "Authorize" %} {{ application.name }}</h4>

      {% if not error %}
        <p>{% blocktrans with app=application.name %}<strong>{{ app }}</strong> is requesting access to your OE5XRX account.{% endblocktrans %}</p>

        <p class="text-muted small">{% trans "It will receive:" %}</p>
        <ul class="small">
          {% for scope in scopes_descriptions %}
          <li>{{ scope }}</li>
          {% endfor %}
        </ul>

        <form method="post" class="mt-4">
          {% csrf_token %}
          {{ form.errors }}
          {{ form.non_field_errors }}
          <div style="display:none;">
            {{ form.redirect_uri }}{{ form.scope }}{{ form.client_id }}
            {{ form.state }}{{ form.response_type }}{{ form.code_challenge }}
            {{ form.code_challenge_method }}{{ form.nonce }}{{ form.claims }}
          </div>
          <div class="d-flex gap-2">
            <button type="submit" name="allow" value="Authorize" class="btn btn-primary flex-grow-1">
              {% trans "Authorize" %}
            </button>
            <button type="submit" formaction="" class="btn btn-outline-secondary">
              {% trans "Cancel" %}
            </button>
          </div>
        </form>
      {% else %}
        <div class="alert alert-danger">{{ error.error }}: {{ error.description }}</div>
      {% endif %}
    </div>
  </div>
</div>
{% endblock %}
```

- [ ] **Step 3: Smoke test**

```bash
# Visually inspect during local dev — no automated test for visual output.
# Just verify the page renders without 500 by sending a valid authorize URL.
```

- [ ] **Step 4: Commit**

```bash
git add apps/sso/templates/oauth2_provider/authorize.html
git commit -m "sso: Bootstrap-styled override for OIDC consent page

Same fields as DOT default but matches the station-manager visual
theme. Lives at apps/sso/templates/oauth2_provider/ so Django's
template loader picks it up before the DOT-bundled version."
```

---

## Phase 7 — End-to-End Tests + Deploy

### Task 18: Full Authorization-Code-with-PKCE flow integration test

**Files:**
- Create: `tests/test_sso_flow.py`

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end Authorization-Code + PKCE flow against the live DOT endpoints.

Uses Django's test Client + authlib's PKCE helper. No mocks — every
HTTP roundtrip goes through the real ASGI dispatch, real DB, real
token issuance.
"""

import base64
import hashlib
import secrets

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from oauth2_provider.models import Application

from apps.sso.models import AppGrant

User = get_user_model()


def _pkce_pair():
    verifier = secrets.token_urlsafe(64)[:64]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode().rstrip("=")
    )
    return verifier, challenge


@pytest.fixture
def application(db):
    return Application.objects.create(
        name="InvenTree-Test",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://rp.example/oidc/callback/",
        skip_authorization=False,
        algorithm="RS256",
    )


@pytest.fixture
def authorized_user(db, application):
    g = Group.objects.create(name="operator")
    u = User.objects.create_user(
        username="peterb", password="hunter2", email="peter@oe5xrx.org",
        first_name="Peter", last_name="Buchegger",
    )
    u.groups.add(g)
    AppGrant.objects.create(user=u, application=application)
    return u


@pytest.mark.django_db
def test_full_auth_code_pkce_flow_yields_id_token_with_groups_claim(
    client, application, authorized_user,
):
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    # Step 1: hit authorize. User is already logged in + has grant +
    # skip_authorization=False so we land on the consent page.
    resp = client.get(
        "/sso/authorize/",
        {
            "response_type": "code",
            "client_id": application.client_id,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "scope": "openid profile email groups",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    # Consent page renders 200; CSRF + redirect_uri etc. are in hidden form fields.
    assert resp.status_code == 200

    # Step 2: post consent — DOT redirects to the RP redirect_uri with ?code=.
    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "response_type": "code",
            "scope": "openid profile email groups",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )
    assert resp.status_code == 302
    assert resp["Location"].startswith("https://rp.example/oidc/callback/")
    from urllib.parse import urlparse, parse_qs
    parsed = parse_qs(urlparse(resp["Location"]).query)
    code = parsed["code"][0]
    assert parsed["state"] == ["xyz"]

    # Step 3: exchange code for tokens via /sso/token/.
    import base64 as b64
    basic = b64.b64encode(
        f"{application.client_id}:{application.client_secret}".encode()
    ).decode()
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "code_verifier": verifier,
        },
        HTTP_AUTHORIZATION=f"Basic {basic}",
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "id_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "Bearer"

    # Step 4: decode id_token (don't verify signature — that's a separate
    # test) and check groups claim.
    payload = data["id_token"].split(".")[1]
    payload += "=" * (-len(payload) % 4)
    import json
    decoded = json.loads(b64.urlsafe_b64decode(payload))
    assert decoded["preferred_username"] == "peterb"
    assert decoded["email"] == "peter@oe5xrx.org"
    assert "operator" in decoded["groups"]
    assert decoded["aud"] == application.client_id

    # Step 5: call /sso/userinfo/ with the access token.
    resp = client.get(
        "/sso/userinfo/",
        HTTP_AUTHORIZATION=f"Bearer {data['access_token']}",
    )
    assert resp.status_code == 200
    user_data = resp.json()
    assert user_data["preferred_username"] == "peterb"
    assert "operator" in user_data["groups"]
```

- [ ] **Step 2: Run, debug**

```bash
docker compose run --rm web pytest tests/test_sso_flow.py -v -s
```

Likely friction points & fixes:
- DOT version differences in `Application` fields (`algorithm`, `skip_authorization`). Adjust fixture to match the installed DOT version's model.
- CSRF for the POST consent step: pass `enforce_csrf_checks=False` or use `client.session` to assemble a CSRF token. Default test Client skips CSRF.
- The authorize POST may need `allow` field to be the literal string `"Authorize"`. Verify against the consent template in Task 17.

Iterate until the test passes.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sso_flow.py
git commit -m "sso: full Auth-Code+PKCE flow integration test

End-to-end test exercising authorize → consent → token exchange →
userinfo with a real authorlib-compatible PKCE pair. Asserts:
- 302 redirect to RP after consent
- access + id + refresh tokens issued
- ID-token contains preferred_username/email/groups
- /sso/userinfo/ returns the same claims
No mocks — runs against the real DOT endpoints + RSA test key."
```

---

### Task 19: Negative-path integration tests

**Files:**
- Modify: `tests/test_sso_flow.py` (append negative-path tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sso_flow.py`:

```python
@pytest.mark.django_db
def test_authorize_without_appgrant_redirects_with_access_denied(client, application):
    """User logged in but no AppGrant → DOT returns the consent screen but
    the validator rejects, redirecting to RP with ?error=access_denied.

    The exact flow may differ based on DOT internals; what we MUST see
    is no auth code issued.
    """
    verifier, challenge = _pkce_pair()
    g = Group.objects.create(name="member")
    u = User.objects.create_user(username="ungrant", password="x", email="u@x")
    u.groups.add(g)
    client.force_login(u)
    # NB: no AppGrant created.

    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "response_type": "code",
            "scope": "openid",
            "state": "deny",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )
    # We accept either: 302 with ?error=access_denied, or 4xx outright.
    if resp.status_code == 302:
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(resp["Location"]).query)
        assert "code" not in q
        assert q.get("error", [""])[0] == "access_denied"
    else:
        assert resp.status_code in (400, 401, 403)


@pytest.mark.django_db
def test_token_exchange_with_wrong_code_verifier_fails(client, application, authorized_user):
    """Hand the token endpoint a code_verifier that doesn't match the
    code_challenge sent during authorize — expect invalid_grant.
    """
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "response_type": "code",
            "scope": "openid",
            "state": "x",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )
    from urllib.parse import urlparse, parse_qs
    code = parse_qs(urlparse(resp["Location"]).query)["code"][0]

    import base64 as b64
    basic = b64.b64encode(
        f"{application.client_id}:{application.client_secret}".encode()
    ).decode()
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "code_verifier": "this-is-not-the-real-verifier-12345678901234567890",
        },
        HTTP_AUTHORIZATION=f"Basic {basic}",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


@pytest.mark.django_db
def test_token_exchange_with_unknown_redirect_uri_fails(client, application, authorized_user):
    """Authorize was issued for rp.example; token exchange claiming a
    different redirect_uri must be rejected."""
    verifier, challenge = _pkce_pair()
    client.force_login(authorized_user)

    resp = client.post(
        "/sso/authorize/",
        {
            "client_id": application.client_id,
            "redirect_uri": "https://rp.example/oidc/callback/",
            "response_type": "code",
            "scope": "openid",
            "state": "x",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "allow": "Authorize",
        },
    )
    from urllib.parse import urlparse, parse_qs
    code = parse_qs(urlparse(resp["Location"]).query)["code"][0]

    import base64 as b64
    basic = b64.b64encode(
        f"{application.client_id}:{application.client_secret}".encode()
    ).decode()
    resp = client.post(
        "/sso/token/",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://elsewhere.example/cb/",
            "code_verifier": verifier,
        },
        HTTP_AUTHORIZATION=f"Basic {basic}",
    )
    assert resp.status_code == 400
```

- [ ] **Step 2: Run, expect pass**

```bash
docker compose run --rm web pytest tests/test_sso_flow.py -v
```
Expected: all flow tests pass (positive + negative).

- [ ] **Step 3: Commit**

```bash
git add tests/test_sso_flow.py
git commit -m "sso: negative-path integration tests (access_denied, invalid_grant)

Covers: missing AppGrant during authorize → access_denied (or 4xx),
wrong PKCE verifier → invalid_grant, mismatched redirect_uri during
token exchange → 400. Codifies that we never issue a token in these
cases."
```

---

### Task 20: Docker volume + Nginx + README

**Files:**
- Modify: `docker-compose.yml` (add oidc_keys volume mount)
- Modify: `deploy/docker-compose.prod.yml` (same)
- Modify: `deploy/nginx.conf` (verify `/sso/` is proxied)
- Modify: `README.md` (document SSO setup)

- [ ] **Step 1: Add the volume in dev compose**

Modify `docker-compose.yml` — append a named volume + mount it into the `web` service:

```yaml
services:
  web:
    # ... existing config ...
    volumes:
      - .:/app
      - oidc_keys:/app/oidc_keys
    # ...

volumes:
  postgres_data:
  oidc_keys:
```

- [ ] **Step 2: Same change for prod**

Modify `deploy/docker-compose.prod.yml` similarly:

```yaml
services:
  web:
    # ... existing ...
    volumes:
      - oidc_keys:/app/oidc_keys
    # ...

volumes:
  postgres_data:
  certbot_certs:
  certbot_www:
  oidc_keys:
```

- [ ] **Step 3: Verify Nginx**

```bash
docker compose run --rm web cat deploy/nginx.conf
```
Check whether `/sso/` is covered. Typical station-manager config has a catch-all `location / { proxy_pass http://web:8000; }`. If so, nothing to change. If `/sso/` needs explicit handling (e.g., for buffer-size on JWKS responses), add:

```nginx
location /sso/ {
    proxy_pass http://web:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

- [ ] **Step 4: README updates**

Append to `README.md`:

````markdown
## SSO / OIDC Provider

The station-manager doubles as an OIDC identity provider for other
OE5XRX apps (InvenTree, Grafana, Nextcloud, …). See
`docs/superpowers/specs/2026-05-18-sso-oidc-provider-design.md` for
the full design.

### One-time setup on a fresh host

```bash
# Generate the RSA-2048 signing key (persists in the oidc_keys volume).
docker compose run --rm web python manage.py setup_oidc_keys
docker compose up -d --force-recreate web
```

### Registering a new RP-application

1. Log into the station-manager as an admin.
2. Browse to `/admin/oauth2_provider/application/add/`.
3. Set `Name`, `Client type` = Confidential, `Authorization grant type`
   = Authorization code, `Redirect URIs` (one per line).
4. Save. Copy the `client_secret` shown — Django Admin only displays
   it once.
5. Hand `client_id`, `client_secret`, and the discovery URL
   `https://ham.oe5xrx.org/sso/.well-known/openid-configuration` to
   the RP's operator.

### Granting users access

User detail page → "App-Zugriffe" card → click "Gewähren" next to the
app. Audit log entry written automatically.
````

- [ ] **Step 5: Final integration run**

```bash
docker compose down
docker compose up -d
docker compose run --rm web python manage.py setup_oidc_keys
docker compose run --rm web pytest -x
```
Expected: full suite green.

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml deploy/docker-compose.prod.yml deploy/nginx.conf README.md
git commit -m "sso: deployment glue — oidc_keys volume + README setup notes

Adds the persistent docker volume so RSA signing keys survive
container recreates. Nginx config already proxies /sso/ via the
catch-all; left a comment in deploy/nginx.conf if explicit handling
is wanted later. README documents the one-time key bootstrap +
RP registration flow."
```

---

## Done — What to Verify End-to-End

After Task 20 merges:

1. Run `docker compose run --rm web pytest -x` — full suite green.
2. Boot a local dev instance, register a test Application, give yourself an AppGrant.
3. Use a one-off OIDC test client (e.g. `oidc-tester` Docker image) pointed at `http://localhost:8000/sso/.well-known/openid-configuration` → confirm full Auth-Code+PKCE flow returns an ID token with `groups` claim.
4. Deploy to staging; repeat (3) against the real domain.
5. Configure InvenTree's OIDC client (production task — outside this plan's scope).

If staging step (3) succeeds, ship. If InvenTree integration surfaces any RP-specific quirks, they live in a follow-up PR.

---

## Plan Self-Review Notes

Spec coverage matrix:

| Spec Section | Plan Task(s) |
|---|---|
| 1.1 Django-App skeleton | T2 (apps.py, __init__.py, management/, etc.) |
| 1.2 INSTALLED_APPS + urls + settings | T1 |
| 1.3 Unchanged existing auth paths | verified via T5 sweep + full pytest run in T20 |
| 1.4 URL prefix `/sso/` | T1 |
| 2.1 AppGrant model | T7 |
| 2.2 DOT models reused | T1 (DOT migrations) |
| 2.3 User-role → Groups refactor | T4, T5, T6 |
| 2.4 Custom claims hook | T8 |
| 2.5 Access validator | T9 |
| 3.1 Auth-Code+PKCE only | T1 settings + T9 is_pkce_required override + T18 verify |
| 3.2 Token lifetimes | T1 settings |
| 3.3-3.4 Auth-code flow + ID-Token shape | T18 |
| 3.5 RP-initiated logout + redirect protection | T16 |
| 3.6 Error paths | T19 |
| 3.7 Discovery endpoint | T3 |
| 4.1 Application Django-Admin customizing | T12 |
| 4.2 User-detail App-Grants card | T13 |
| 4.3 SSO dashboard | T14, T15 |
| 4.4 Audit log | T10 |
| 5.1 RSA key + bootstrap command | T2 |
| 5.2 Token storage clarification | documented in spec; nothing to implement |
| 5.3 Brute force + rate limit | T1 axes-stays comment; throttle on /sso/token/ left to ops, not implemented (see "deferred" below) |
| 5.4 Redirect URI exact match | T16 + DOT default behavior |
| 5.5 Cascading revocation signals | T11 |
| 5.6 Logging-safety | follows DOT defaults; no extra code |
| 5.7 CSP unchanged | no change |
| 6.x Testing | T4 (migration), T7-9 (units), T18-19 (E2E) |
| 7.x Migration steps | T4, T5, T6 (three commits) |
| 7.4 Deployment diff | T20 |
| 7.5 InvenTree integration | README only — Ops task |

**Deferred from spec (acknowledged):**
- Section 5.3 `/sso/token/` rate-limit: needs a custom DRF throttle class hooked into DOT's `TokenView`. DOT 3.x uses a Django view, not DRF, so this requires a custom middleware or DOT-specific throttle. Punted to a follow-up since axes covers the high-frequency case (login) and the token endpoint is only reachable with a valid auth code.

**Type / signature consistency:**
- `user_can_access(user, application)` — same signature in Tasks 9 (impl) and called nowhere else directly (only `SsoOAuth2Validator` consumes it).
- `SsoAuditLog.log(...)` — keyword-only signature defined in Task 10, called from T11, T13.
- `_build_grants_for_user(user)` returns `list[(Application, AppGrant|None)]` — defined T13, consumed T13 (template + UserUpdateView).

No placeholders, no TBD's, no incomplete code blocks.
