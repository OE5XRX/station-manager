# Alert-Rules Seed + Empty-State — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `apps/monitoring` alert engine functional on every fresh / DR-restored DB by seeding the 6 system-managed `AlertRule` rows in a data migration, and surface a self-diagnostic empty-state in the settings UI for the (now-impossible-but-defensive) case of zero rules.

**Architecture:** A `RunPython` data migration owns the seed (idempotent `get_or_create` per `alert_type`), replacing the never-invoked `create_default_alert_rules` management command's role at deploy time. The command stays as an operator escape-hatch. Test fixtures switch from `create()` to `update_or_create()` to coexist with migration-seeded rows. Template gets a `{% empty %}` info-banner.

**Tech Stack:** Django 6.0 data-migration with `RunPython`, pytest-django, HTML/Jinja-ish DTL templates.

**Reference context (no spec document — this is a small set of defects discussed in conversation 2026-06-05):**
- `apps/monitoring/models.py:8` — `AlertRule.alert_type` is `unique=True` constrained to a `TextChoices` enum of 6 values.
- `apps/monitoring/engine.py:19-24` — `_get_active_rule` returns `None` if no rule exists, which silently skips every check.
- `apps/monitoring/management/commands/create_default_alert_rules.py` — already contains the canonical seed data; reference values only, the migration carries its own copy.
- `apps/accounts/migrations/0002_role_to_groups.py` — local reference pattern for an idempotent data migration with a safe reverse.
- `tests/conftest.py:197-254` — existing AlertRule fixtures use `objects.create()`; will collide with seeded rows after this PR lands.
- `tests/test_monitoring.py` — 232 lines of engine tests already exist; this plan adds a single new test for the migration itself plus a template smoke test.

---

## Task 1: Failing test for the seed migration

**Files:**
- Create: `tests/test_monitoring_seed.py`

The test asserts that the default rules exist in a freshly-migrated DB. Runs against the test session DB which pytest-django builds by replaying all migrations, so the rows must be present at session start without any fixture creating them. A bare `@pytest.mark.django_db` is enough — no fixture references means no fixture-created rows could mask a missing migration.

- [ ] **Step 1: Write the failing test**

Create `tests/test_monitoring_seed.py`:

```python
"""Verify the data migration seeds the system-managed AlertRule set.

These rules are not user-creatable (alert_type is unique + restricted to a
TextChoices enum, engine.py has one hard-coded check per type). Without the
seed migration, every check_alerts() tick silently no-ops because
_get_active_rule() returns None for every type. This test exists so a
future refactor that drops or skips the migration fails CI loudly.
"""

import pytest

from apps.monitoring.models import AlertRule

EXPECTED_RULES = {
    AlertRule.AlertType.STATION_OFFLINE: {
        "threshold": 5.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.CPU_TEMPERATURE: {
        "threshold": 80.0,
        "severity": AlertRule.Severity.WARNING,
    },
    AlertRule.AlertType.DISK_WARNING: {
        "threshold": 90.0,
        "severity": AlertRule.Severity.WARNING,
    },
    AlertRule.AlertType.DISK_CRITICAL: {
        "threshold": 95.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.RAM_CRITICAL: {
        "threshold": 90.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
    AlertRule.AlertType.OTA_FAILED: {
        "threshold": 0.0,
        "severity": AlertRule.Severity.CRITICAL,
    },
}


@pytest.mark.django_db
def test_all_default_rules_exist():
    """Every alert_type in EXPECTED_RULES has a row in the DB."""
    actual_types = set(AlertRule.objects.values_list("alert_type", flat=True))
    expected_types = set(EXPECTED_RULES.keys())
    assert actual_types >= expected_types, (
        f"Missing seeded rules: {expected_types - actual_types}"
    )


@pytest.mark.django_db
def test_default_rules_have_expected_threshold_and_severity():
    """Seeded rules carry the documented threshold + severity defaults."""
    for alert_type, expected in EXPECTED_RULES.items():
        rule = AlertRule.objects.get(alert_type=alert_type)
        assert rule.threshold == expected["threshold"], (
            f"{alert_type}: threshold {rule.threshold} != {expected['threshold']}"
        )
        assert rule.severity == expected["severity"], (
            f"{alert_type}: severity {rule.severity} != {expected['severity']}"
        )


@pytest.mark.django_db
def test_default_rules_are_active():
    """Seeded rules are is_active=True so the engine picks them up."""
    for alert_type in EXPECTED_RULES.keys():
        rule = AlertRule.objects.get(alert_type=alert_type)
        assert rule.is_active is True, f"{alert_type}: is_active=False"
```

- [ ] **Step 2: Run the new test to verify it fails**

```bash
pytest tests/test_monitoring_seed.py -v
```

Expected: 3 FAILs — `test_all_default_rules_exist` reports `Missing seeded rules: {...all 6...}`, the other two raise `AlertRule.DoesNotExist`.

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_monitoring_seed.py
git commit -m "test(monitoring): assert default AlertRules exist post-migrate (red)"
```

---

## Task 2: The data migration

**Files:**
- Create: `apps/monitoring/migrations/0002_seed_default_rules.py`

Follow the local pattern from `apps/accounts/migrations/0002_role_to_groups.py`: define the data inline (not imported from `models` — migrations must be replayable against historical schemas), use `apps.get_model()` to fetch the model frozen at this migration point, `get_or_create` with `defaults={...}` so operators who later tune `threshold`/`is_active` via the UI never get overwritten by a re-run, and a safe noop reverse so a rollback never loses operator data.

- [ ] **Step 1: Create the migration**

Create `apps/monitoring/migrations/0002_seed_default_rules.py`:

```python
"""Seed the 6 system-managed AlertRule rows.

The engine in apps/monitoring/engine.py has one hard-coded check per
AlertType — without these rows, _get_active_rule() returns None for every
type and every check_alerts() tick silently no-ops. The matching
management command (apps/monitoring/management/commands/
create_default_alert_rules.py) was never wired into a deploy step, so
fresh DBs (incl. DR-restores from an empty snapshot) end up with a dead
alert-monitor loop. This migration closes that gap on every `migrate`
run.

Idempotent: get_or_create matches on the unique alert_type field. An
operator who later tunes threshold/severity/is_active via the UI is
never overwritten by a re-run — defaults only apply on first creation.

Reverse — DATA-LOSS WARNING
---------------------------
Reverse is a noop. Removing seeded rows on rollback would discard any
operator-tuned values that landed on top of them, and the engine
collapses to a silent no-op the moment the rows are gone. If a future
migration genuinely needs to remove a rule, write an explicit data
migration that targets just that alert_type.
"""

from django.db import migrations

DEFAULT_RULES = [
    {
        "alert_type": "station_offline",
        "threshold": 5.0,
        "severity": "critical",
        "description": "Station has not sent a heartbeat for more than 5 minutes.",
    },
    {
        "alert_type": "cpu_temperature",
        "threshold": 80.0,
        "severity": "warning",
        "description": "CPU temperature exceeds 80 degrees Celsius.",
    },
    {
        "alert_type": "disk_warning",
        "threshold": 90.0,
        "severity": "warning",
        "description": "Disk usage exceeds 90% (less than 10% free).",
    },
    {
        "alert_type": "disk_critical",
        "threshold": 95.0,
        "severity": "critical",
        "description": "Disk usage exceeds 95% (less than 5% free).",
    },
    {
        "alert_type": "ram_critical",
        "threshold": 90.0,
        "severity": "critical",
        "description": "RAM usage exceeds 90%.",
    },
    {
        "alert_type": "ota_failed",
        "threshold": 0.0,
        "severity": "critical",
        "description": "An OTA deployment failed or was rolled back.",
    },
]


def seed_default_rules(apps, schema_editor):
    AlertRule = apps.get_model("monitoring", "AlertRule")
    for spec in DEFAULT_RULES:
        AlertRule.objects.get_or_create(
            alert_type=spec["alert_type"],
            defaults={
                "threshold": spec["threshold"],
                "severity": spec["severity"],
                "description": spec["description"],
                "is_active": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            seed_default_rules,
            reverse_code=migrations.RunPython.noop,
        ),
    ]
```

- [ ] **Step 2: Sanity-check that Django sees the migration and `makemigrations` is clean**

```bash
python manage.py makemigrations --check --dry-run monitoring
```

Expected: `No changes detected in app 'monitoring'` (exit 0). If Django suggests a new migration, something is off — investigate before continuing.

- [ ] **Step 3: Run the seed tests — they must now pass**

```bash
pytest tests/test_monitoring_seed.py -v
```

Expected: 3 PASSes.

- [ ] **Step 4: Commit the migration**

```bash
git add apps/monitoring/migrations/0002_seed_default_rules.py
git commit -m "feat(monitoring): seed 6 system-managed AlertRules in data migration (green)"
```

---

## Task 3: Make existing test fixtures coexist with the seed

**Files:**
- Modify: `tests/conftest.py:197-254`

The 5 `*_alert_rule` fixtures currently call `AlertRule.objects.create(...)`. After Task 2 lands, the seed migration creates the same `alert_type` rows at session start. Each fixture's `create()` will now raise `IntegrityError` on the `unique=True` constraint on `alert_type`. Fix: switch each to `update_or_create()` with `defaults=` matching the fixture's stated values, so the fixture either creates the row (if the migration hasn't seeded it — defensive) or overwrites the relevant fields on the seeded row (deterministic, scoped to the test session via the auto-rollback transaction).

- [ ] **Step 1: Refactor `offline_alert_rule`**

In `tests/conftest.py`, replace the body of `offline_alert_rule`:

```python
@pytest.fixture
def offline_alert_rule(db):
    """An active AlertRule for station_offline.

    Uses update_or_create so it coexists with the seed migration
    (apps/monitoring/migrations/0002_seed_default_rules.py) that creates
    this row at session start. defaults= forces the fixture's documented
    values regardless of any per-test mutation from earlier tests in the
    same session — pytest-django's auto-rollback only undoes the test's
    own writes, not migration data.
    """
    rule, _ = AlertRule.objects.update_or_create(
        alert_type=AlertRule.AlertType.STATION_OFFLINE,
        defaults={
            "threshold": 0,
            "severity": AlertRule.Severity.CRITICAL,
            "is_active": True,
            "description": "Station offline check",
        },
    )
    return rule
```

- [ ] **Step 2: Refactor `cpu_temp_alert_rule`**

```python
@pytest.fixture
def cpu_temp_alert_rule(db):
    """An active AlertRule for cpu_temperature with threshold 80."""
    rule, _ = AlertRule.objects.update_or_create(
        alert_type=AlertRule.AlertType.CPU_TEMPERATURE,
        defaults={
            "threshold": 80.0,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
            "description": "CPU temperature check",
        },
    )
    return rule
```

- [ ] **Step 3: Refactor `disk_warning_alert_rule`**

```python
@pytest.fixture
def disk_warning_alert_rule(db):
    """An active AlertRule for disk_warning with threshold 90."""
    rule, _ = AlertRule.objects.update_or_create(
        alert_type=AlertRule.AlertType.DISK_WARNING,
        defaults={
            "threshold": 90.0,
            "severity": AlertRule.Severity.WARNING,
            "is_active": True,
            "description": "Disk warning check",
        },
    )
    return rule
```

- [ ] **Step 4: Refactor `ram_critical_alert_rule`**

```python
@pytest.fixture
def ram_critical_alert_rule(db):
    """An active AlertRule for ram_critical with threshold 90."""
    rule, _ = AlertRule.objects.update_or_create(
        alert_type=AlertRule.AlertType.RAM_CRITICAL,
        defaults={
            "threshold": 90.0,
            "severity": AlertRule.Severity.CRITICAL,
            "is_active": True,
            "description": "RAM critical check",
        },
    )
    return rule
```

- [ ] **Step 5: Refactor `ota_failed_alert_rule`**

```python
@pytest.fixture
def ota_failed_alert_rule(db):
    """An active AlertRule for ota_failed."""
    rule, _ = AlertRule.objects.update_or_create(
        alert_type=AlertRule.AlertType.OTA_FAILED,
        defaults={
            "threshold": 0,
            "severity": AlertRule.Severity.CRITICAL,
            "is_active": True,
            "description": "OTA failure check",
        },
    )
    return rule
```

- [ ] **Step 6: Run the full monitoring test suite to verify no regressions**

```bash
pytest tests/test_monitoring.py tests/test_monitoring_seed.py -v
```

Expected: all PASS (the engine tests + the 3 new seed tests). If any engine test fails with `IntegrityError`, double-check the fixture conversion — the symptom is a fixture that still calls `create()` instead of `update_or_create()`.

- [ ] **Step 7: Commit the fixture refactor**

```bash
git add tests/conftest.py
git commit -m "test(monitoring): switch alert-rule fixtures to update_or_create"
```

---

## Task 4: Failing test for the template empty-state

**Files:**
- Modify: `tests/test_monitoring.py` (append a new test class)

The empty-state is mostly defensive (the migration always seeds 6 rules, so the production UI will not encounter it under normal operation). Worth adding because: (a) operators can manually delete rules via Django Admin; (b) a future schema-migration that requires temporary rule-deletion would otherwise expose a blank table with no diagnostic; (c) the test pins the operator-facing copy so future template edits don't accidentally remove it.

- [ ] **Step 1: Append the failing test**

Append to the end of `tests/test_monitoring.py`:

```python
@pytest.mark.django_db
class TestAlertSettingsEmptyState:
    """The settings page shows a diagnostic banner when no rules exist.

    Production should never hit this state (migration 0002 seeds 6 rows),
    but Django Admin allows manual deletion. The empty-state is the
    operator's signal that the seed migration didn't run or was undone.
    """

    def test_empty_table_shows_diagnostic_banner(self, admin_client):
        """No AlertRule rows -> banner with 'init container' diagnostic."""
        from apps.monitoring.models import AlertRule

        AlertRule.objects.all().delete()
        response = admin_client.get(reverse("monitoring:alert_settings"))

        assert response.status_code == 200
        content = response.content.decode()
        # Pin the operator-facing diagnostic phrase. If this assertion
        # fails because someone reworded the banner, update the literal
        # below — but make sure the new wording still points the operator
        # at the init container.
        assert "init container" in content.lower(), (
            "Empty-state banner missing diagnostic hint — "
            "operators need a pointer to the init container logs."
        )

    def test_seeded_table_does_not_show_banner(self, admin_client):
        """When rules exist (default state), the banner is absent."""
        response = admin_client.get(reverse("monitoring:alert_settings"))

        assert response.status_code == 200
        content = response.content.decode()
        assert "init container" not in content.lower()
```

The `admin_client` fixture comes from pytest-django and authenticates as a Django superuser; combined with the existing `AdminRequiredMixin`-protected view, it gets past the permission check. (Verify: `grep "admin_client" tests/` to confirm it's already used elsewhere in the suite — if not, fall back to logging in `admin_user` explicitly.)

- [ ] **Step 2: Run the new test class to verify it fails**

```bash
pytest tests/test_monitoring.py::TestAlertSettingsEmptyState -v
```

Expected: `test_empty_table_shows_diagnostic_banner` FAILs (banner doesn't exist yet), `test_seeded_table_does_not_show_banner` PASSes incidentally.

If `admin_client` is undefined, see the verification note above and switch to:

```python
def test_empty_table_shows_diagnostic_banner(self, client, admin_user):
    client.force_login(admin_user)
    AlertRule.objects.all().delete()
    response = client.get(reverse("monitoring:alert_settings"))
    # ... assertions identical to above
```

- [ ] **Step 3: Commit the failing test**

```bash
git add tests/test_monitoring.py
git commit -m "test(monitoring): assert empty-state banner on /monitoring/settings/ (red)"
```

---

## Task 5: Implement the template empty-state

**Files:**
- Modify: `apps/monitoring/templates/monitoring/alert_settings.html:34-66`

Add a `{% empty %}` branch to the existing `{% for rule in alert_rules %}` loop. The banner explains the expected state ("default rules are created by a data migration") and the diagnostic hint ("check init container logs") that the test pins. Keep it inside the table for layout simplicity — one row spanning all 5 columns.

- [ ] **Step 1: Modify the template**

In `apps/monitoring/templates/monitoring/alert_settings.html`, replace the `{% for rule in alert_rules %}` … `{% endfor %}` block (currently lines ~35-66) with the same block plus an `{% empty %}` branch:

```html
{% for rule in alert_rules %}
<tr>
  <td>
    <div class="stack-gap-2">
      <span style="font-weight:600;color:var(--ink-0);">{{ rule.get_alert_type_display }}</span>
      <span class="t-mono-sm t-muted">{{ rule.description|default:rule.alert_type }}</span>
    </div>
  </td>
  <td>
    {% if rule.severity == "critical" %}<span class="pill pill-offline">{% trans "CRITICAL" %}</span>
    {% else %}<span class="pill pill-warn">{% trans "WARNING" %}</span>{% endif %}
  </td>
  <td>
    <form hx-post="{% url 'monitoring:alert_rule_update' rule.pk %}" class="row-gap-8">
      {% csrf_token %}
      <input type="number" step="0.1" name="threshold" value="{{ rule.threshold }}" style="width:120px;">
      <button type="submit" class="btn btn-sm">{% trans "Save" %}</button>
    </form>
  </td>
  <td>
    <form hx-post="{% url 'monitoring:alert_rule_update' rule.pk %}">
      {% csrf_token %}
      <label style="display:inline-flex;align-items:center;gap:6px;text-transform:none;letter-spacing:0;font-family:var(--font-body);font-size:13px;color:var(--ink-0);cursor:pointer;">
        <input type="hidden" name="is_active" value="false">
        <input type="checkbox" name="is_active" value="true" {% if rule.is_active %}checked{% endif %} data-submit-on-change>
        {% if rule.is_active %}{% trans "Enabled" %}{% else %}{% trans "Disabled" %}{% endif %}
      </label>
    </form>
  </td>
  <td></td>
</tr>
{% empty %}
<tr>
  <td colspan="5" style="padding:24px;">
    <div class="stack-gap-2">
      <span style="font-weight:600;color:var(--ink-0);">
        {% trans "No alert rules configured." %}
      </span>
      <span class="t-muted">
        {% blocktrans %}The 6 default rules are created by a data migration on the first deploy. If this message persists after a successful deploy, check the init container logs — the seed migration did not run or was rolled back.{% endblocktrans %}
      </span>
    </div>
  </td>
</tr>
{% endfor %}
```

- [ ] **Step 2: Run the empty-state tests — they must now pass**

```bash
pytest tests/test_monitoring.py::TestAlertSettingsEmptyState -v
```

Expected: both PASS.

- [ ] **Step 3: Run the full monitoring suite as regression-guard**

```bash
pytest tests/test_monitoring.py tests/test_monitoring_seed.py -v
```

Expected: all PASS, no warnings about unused fixtures.

- [ ] **Step 4: Commit the template change**

```bash
git add apps/monitoring/templates/monitoring/alert_settings.html
git commit -m "feat(monitoring): show diagnostic empty-state in alert-rules table (green)"
```

---

## Task 6: Full-suite regression run

**Files:** (none modified)

The fixture refactor in Task 3 touches a shared `conftest.py` that other test files import from. Run the full test suite to catch any non-monitoring test that depends on AlertRule fixtures with the old `create()` semantics.

- [ ] **Step 1: Run the full test suite**

```bash
pytest
```

Expected: all PASS. If a non-monitoring test fails, inspect the failure — most likely it created an AlertRule via direct `objects.create()` somewhere and now collides with the seeded row. Fix by switching that call site to `update_or_create()` with the same pattern as Task 3.

- [ ] **Step 2 (only if a fix was needed in Step 1): commit the additional fix**

```bash
git add <touched files>
git commit -m "test: switch <call site> to update_or_create for seed coexistence"
```

---

## Task 7: Open the PR

**Files:** (none modified)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/monitoring-seed-default-rules-and-empty-state
```

- [ ] **Step 2: Open the PR with `gh pr create`**

```bash
gh pr create --title "feat(monitoring): seed default AlertRules + diagnostic empty-state" --body "$(cat <<'EOF'
## Summary

Closes two real defects in `apps/monitoring/`:

1. **Default AlertRules were never seeded automatically.** The `create_default_alert_rules` management command exists but is not invoked by the `init` container or any workflow. Production today only has rules because someone ran it by hand; a DR-restore from an empty snapshot would leave the table empty and the `alert-monitor` loop silently no-opping forever — no offline-alerts, no disk-full alerts, nothing.
2. **No UI signal when the table is empty.** Operators see a blank table with no diagnostic.

A data migration owns the seed going forward; the command stays as an operator escape-hatch. The settings template gets a `{% empty %}` banner pointing operators at the init-container logs.

## Changes

| File | Change |
|---|---|
| `apps/monitoring/migrations/0002_seed_default_rules.py` | **New** — `RunPython` migration idempotently seeds the 6 system rules. Noop reverse so a rollback never loses operator-tuned values. |
| `apps/monitoring/templates/monitoring/alert_settings.html` | `{% empty %}` branch with diagnostic banner. |
| `tests/conftest.py` | 5 alert-rule fixtures switched from `create()` to `update_or_create()` to coexist with the seeded rows. |
| `tests/test_monitoring_seed.py` | **New** — 3 tests asserting all 6 rules exist with documented threshold/severity/active. |
| `tests/test_monitoring.py` | New `TestAlertSettingsEmptyState` class — 2 tests pinning the empty-state diagnostic and confirming it's hidden in normal state. |

## Test plan

- [x] `pytest tests/test_monitoring_seed.py -v` — all 3 PASS
- [x] `pytest tests/test_monitoring.py -v` — engine + empty-state tests all PASS
- [x] `pytest` — full suite green, no regressions
- [ ] Post-merge: confirm the `init` container's `migrate --noinput` logs `Applying monitoring.0002_seed_default_rules... OK` exactly once on next deploy
- [ ] Post-merge: `/monitoring/settings/` shows all 6 rules in production (visual check)
- [ ] Post-merge: trigger a synthetic offline-alert (stop a station's heartbeat for >5min, or temporarily fudge `last_seen`) → confirm Email arrives via Brevo (`alert-monitor` loop now has rules to act on)

## What this PR does NOT do

- Does not change the `AlertRule` model. The 6-types-only design is intentional — see the conversation that produced this plan. If we ever want free-form user-creatable rules, that's a separate brainstorm/spec.
- Does not touch the `create_default_alert_rules` management command. It stays as an operator escape-hatch (e.g., after a manual cleanup via Django Admin).
- Does not fix the unrelated HTMX-no-feedback bug on the "Send test email" button (deferred per conversation).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Print the PR URL for the operator**

The output of `gh pr create` is the URL — surface it in the final report.

---

## Self-Review

**Spec coverage:**

| Defect | Task |
|---|---|
| Seed never runs automatically | Task 2 (migration) + Task 1 (failing test first) |
| No empty-state UI / no diagnostic | Task 5 (template) + Task 4 (failing test first) |
| Fixture collisions after seed | Task 3 (refactor to update_or_create) |
| Regression risk in other tests | Task 6 (full-suite run) |
| PR delivery | Task 7 |

All discussed defects map to tasks.

**Placeholder scan:** None — every step shows complete code, exact paths, exact commands.

**Type consistency:**
- `AlertRule.AlertType.*` enum values used in tests match the string literals in the migration (`"station_offline"`, etc. — verified against `apps/monitoring/models.py:11-17`).
- Threshold values consistent between Task 1 (`EXPECTED_RULES`), Task 2 (`DEFAULT_RULES`), and the existing `apps/monitoring/management/commands/create_default_alert_rules.py`.
- Migration name `0002_seed_default_rules` referenced in Task 4 docstring matches Task 2's create path.

**Migration replay-safety note:** `apps.get_model("monitoring", "AlertRule")` in Task 2 is required (not `from apps.monitoring.models import AlertRule`) — historical-model fetch is the Django-standard way to make a data migration replayable against any future model change.
