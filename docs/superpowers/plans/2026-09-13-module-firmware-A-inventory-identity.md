# Teilbereich A — Modul-Inventar & Identität — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Module zu UID-getrackten physischen Objekten machen — Typ-Registry, Modul-Entität, Zuordnungs-Historie, Heartbeat-Ingestion mit Auto-Swap, Audit als Modul-Subjekt und die dazugehörigen Sichten — als Fundament für B/C/D.

**Architecture:** Neue Django-App `apps/module_firmware` mit den Identitäts-Modellen. Die bestehende Inventory-Ingestion in `apps/control/registry.apply_inventory` (Chokepoint, aufgerufen aus dem Agent-WebSocket-Consumer) wird erweitert, um pro gemeldetem Modul eine `Module`-Zeile zu pflegen, Slot-Swaps zu erkennen und die Zuordnungs-Historie fortzuschreiben. `StationAuditLog` (in `apps/stations`) wird um einen nullable `module`-FK erweitert, sodass ein Modul auditierbares Subjekt wird. UI: fleet-weite Liste + Modul-Detail + Stations-Integration + Dashboard-Card.

**Tech Stack:** Django 6.0, DRF 3.17, Django Channels, PostgreSQL, Bootstrap 5 + HTMX, pytest + pytest-django, Python 3.14.

**Spec:** `docs/superpowers/specs/2026-09-13-module-firmware-A-inventory-identity-design.md`

## Global Constraints

- **Sprache:** Code-Kommentare/Docstrings Englisch; UI-Copy Deutsch (Projekt-Konvention).
- **Versions-Regel:** Immer neueste stabile Version prüfen, keine alten Defaults.
- **Django-Template-Kommentare:** Multi-line `{# … #}` ist **verboten** — immer `{% comment %}…{% endcomment %}`. CI-Guard aktiv.
- **DE-Locale Number-Inputs:** numerische `<input>` mit forced dot-decimal (`lang="en"`) — hier nur relevant, falls Formularfelder mit Zahlen dazukommen (nicht erwartet).
- **Migrations:** committen; `default_auto_field = BigAutoField`.
- **Tests:** pytest im Repo-`tests/`-Verzeichnis; `DJANGO_SETTINGS_MODULE=config.settings.test`. Ausführen mit `pytest`.
- **Frontend-design-Skill Pflicht** für den Pixel-Agent bei allen UI-Tasks (12–16): `Skill("frontend-design")` vor UI-Arbeit invoken (CLAUDE.md-Regel).
- **Commits:** häufig, ein logischer Schritt pro Commit. Footer `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Zugriffsregel durchgängig:** Lesen = jeder eingeloggte User; Mutationen = admin/staff-only (`user.is_staff`).

---

## File Structure

**Neu (`apps/module_firmware/`):**
- `__init__.py`, `apps.py` — App-Config.
- `models.py` — `ModuleType`, `Module`, `ModuleAssignmentHistory` + Choices.
- `ingest.py` — `ingest_module(...)` + Helfer (Ingestion-Kern, ORM-only, synchron).
- `services.py` — `confirm_registration(...)`, `set_lifecycle(...)`.
- `admin.py` — Admin für die drei Modelle.
- `views.py` — Listen-/Detail-/Mutations-Views.
- `urls.py` — URL-Routing (`/modules/`).
- `migrations/` — Schema-Migrationen.
- `templates/module_firmware/module_list.html`, `module_detail.html`.

**Modifiziert:**
- `config/settings/base.py` — App registrieren.
- `config/urls.py` — `apps.module_firmware.urls` includen.
- `apps/control/models.py` — `StationModule.tracked_module` FK.
- `apps/control/registry.py` — Ingestion-Hook in `apply_inventory`.
- `apps/control/views.py` + Control-Panel-Template — Stations-Integration (Link + Badges).
- `apps/stations/models.py` — `StationAuditLog.module` FK, `station` nullable, neue `EventType`, `log()`-Signatur.
- `apps/dashboard/views.py` + Dashboard-Template — Modul-Card.
- `tests/fake_fw.py` — synthetische UID im DESCRIBE.

**Datenfluss der Ingestion (Referenz für alle Ingestion-Tasks):**
`apply_inventory(station, slots)` erhält `slots = [{"slot": int, "control": str, "modules": [{"module": id, "identity": {type, model, version, uid?, uid_source?}, "capabilities": [...], "state": {...}}]}]`. Pro Modul-Eintrag wird nach dem bestehenden `StationModule`-Upsert `ingest_module(...)` aufgerufen; dessen Rückgabe (`Module | None`) wird als `StationModule.tracked_module` verlinkt.

---

## Task 1: App-Scaffold + `ModuleType`-Modell

**Files:**
- Create: `apps/module_firmware/__init__.py`, `apps/module_firmware/apps.py`, `apps/module_firmware/models.py`
- Modify: `config/settings/base.py` (App-Liste)
- Test: `tests/module_firmware/test_module_type.py`, `tests/module_firmware/__init__.py`

**Interfaces:**
- Produces: `apps.module_firmware.models.ModuleType(key: SlugField unique, display_name: str, hw_repo: str)`; `__str__ → display_name or key`.

- [ ] **Step 1: `apps/module_firmware/apps.py`**

```python
from django.apps import AppConfig


class ModuleFirmwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.module_firmware"
    verbose_name = "Module Firmware"

    def ready(self):
        from . import signals  # noqa: F401  (registered in a later task; import is safe once created)
```

> Falls `signals` in Task 6/7 noch nicht existiert, den Import erst dort hinzufügen. Für Task 1 den `ready()`-Body leer lassen (`pass`) und den Import in Task 7 nachziehen.

Für Task 1 stattdessen:

```python
    def ready(self):
        pass
```

- [ ] **Step 2: `apps/module_firmware/__init__.py`** — leer.

- [ ] **Step 3: `apps/module_firmware/models.py` (ModuleType)**

```python
from django.db import models
from django.utils.translation import gettext_lazy as _


class ModuleType(models.Model):
    """Registry of flashable module types (only firmware-bearing types)."""

    key = models.SlugField(_("key"), unique=True, help_text=_("z. B. fm, power, device-tester"))
    display_name = models.CharField(_("display name"), max_length=128)
    hw_repo = models.CharField(_("hardware repo"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module type")
        verbose_name_plural = _("module types")
        ordering = ["key"]

    def __str__(self):
        return self.display_name or self.key
```

- [ ] **Step 4: App registrieren** — in `config/settings/base.py` `"apps.module_firmware",` zur `INSTALLED_APPS`/lokalen App-Liste hinzufügen (bei den anderen `apps.*`, z. B. nach `"apps.images",`).

- [ ] **Step 5: Migration erzeugen**

Run: `python manage.py makemigrations module_firmware`
Expected: `0001_initial.py` mit `ModuleType`.

- [ ] **Step 6: Failing test**

```python
# tests/module_firmware/test_module_type.py
import pytest
from apps.module_firmware.models import ModuleType


@pytest.mark.django_db
def test_module_type_str_and_unique_key():
    t = ModuleType.objects.create(key="fm", display_name="FM Transceiver")
    assert str(t) == "FM Transceiver"
    with pytest.raises(Exception):
        ModuleType.objects.create(key="fm", display_name="Duplicate")
```

- [ ] **Step 7: Run** — `pytest tests/module_firmware/test_module_type.py -v` → PASS (Modell existiert bereits aus Step 3).

- [ ] **Step 8: Commit**

```bash
git add apps/module_firmware/ config/settings/base.py tests/module_firmware/
git commit -m "feat(module_firmware): scaffold app + ModuleType registry"
```

---

## Task 2: `Module`-Modell

**Files:**
- Modify: `apps/module_firmware/models.py`
- Test: `tests/module_firmware/test_module_model.py`

**Interfaces:**
- Produces: `Module(uid: str unique, uid_source: Module.UidSource, module_type: FK ModuleType, lifecycle_status: Module.Lifecycle, registration_status: Module.Registration, last_reported_version: str, first_seen, last_seen, notes)`. Choices: `UidSource.STM32_UID="stm32_uid"|SYNTHETIC="synthetic"`; `Lifecycle.READY="ready"|DEPLOYED="deployed"|DEFECT="defect"|IN_LAB="in_lab"|RETIRED="retired"`; `Registration.UNREGISTERED="unregistered"|REGISTERED="registered"`. Klassenkonstante `Lifecycle.STICKY = {DEFECT, IN_LAB, RETIRED}` als Modulvariable.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_module_model.py
import pytest
from apps.module_firmware.models import Module, ModuleType


@pytest.fixture
def fm_type(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.mark.django_db
def test_module_defaults_and_orthogonal_status(fm_type):
    m = Module.objects.create(uid="ABC123", module_type=fm_type)
    assert m.uid_source == Module.UidSource.STM32_UID
    assert m.lifecycle_status == Module.Lifecycle.READY
    assert m.registration_status == Module.Registration.UNREGISTERED
    # orthogonal: can be deployed AND unregistered
    m.lifecycle_status = Module.Lifecycle.DEPLOYED
    m.save()
    assert m.registration_status == Module.Registration.UNREGISTERED


@pytest.mark.django_db
def test_module_uid_unique(fm_type):
    Module.objects.create(uid="DUP", module_type=fm_type)
    with pytest.raises(Exception):
        Module.objects.create(uid="DUP", module_type=fm_type)
```

- [ ] **Step 2: Run** → FAIL (`Module` fehlt).

- [ ] **Step 3: `Module` in `apps/module_firmware/models.py` ergänzen**

```python
class Module(models.Model):
    """A physical, UID-tracked module. UID is the sole identity."""

    class UidSource(models.TextChoices):
        STM32_UID = "stm32_uid", _("STM32 UID")
        SYNTHETIC = "synthetic", _("Synthetic (Sim)")

    class Lifecycle(models.TextChoices):
        READY = "ready", _("Ready")
        DEPLOYED = "deployed", _("Deployed")
        DEFECT = "defect", _("Defect")
        IN_LAB = "in_lab", _("In Lab")
        RETIRED = "retired", _("Retired")

    class Registration(models.TextChoices):
        UNREGISTERED = "unregistered", _("Unregistered")
        REGISTERED = "registered", _("Registered")

    # Lifecycle states that are operator-set and never auto-overridden by ingestion.
    STICKY_LIFECYCLE = {Lifecycle.DEFECT, Lifecycle.IN_LAB, Lifecycle.RETIRED}

    uid = models.CharField(_("UID"), max_length=128, unique=True)
    uid_source = models.CharField(
        _("UID source"), max_length=16, choices=UidSource.choices, default=UidSource.STM32_UID
    )
    module_type = models.ForeignKey(
        ModuleType, verbose_name=_("module type"), on_delete=models.PROTECT, related_name="modules"
    )
    lifecycle_status = models.CharField(
        _("lifecycle"), max_length=16, choices=Lifecycle.choices, default=Lifecycle.READY
    )
    registration_status = models.CharField(
        _("registration"), max_length=16,
        choices=Registration.choices, default=Registration.UNREGISTERED,
    )
    last_reported_version = models.CharField(_("last reported version"), max_length=64, blank=True)
    first_seen = models.DateTimeField(_("first seen"), null=True, blank=True)
    last_seen = models.DateTimeField(_("last seen"), null=True, blank=True)
    notes = models.TextField(_("notes"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module")
        verbose_name_plural = _("modules")
        ordering = ["module_type", "uid"]
        indexes = [
            models.Index(fields=["uid"]),
            models.Index(fields=["registration_status"]),
            models.Index(fields=["lifecycle_status"]),
        ]

    def __str__(self):
        return f"{self.module_type.key}:{self.uid}"
```

- [ ] **Step 4: Migration** — `python manage.py makemigrations module_firmware`

- [ ] **Step 5: Run** — `pytest tests/module_firmware/test_module_model.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/module_firmware/models.py apps/module_firmware/migrations/ tests/module_firmware/test_module_model.py
git commit -m "feat(module_firmware): Module entity (UID identity, orthogonal lifecycle/registration)"
```

---

## Task 3: `ModuleAssignmentHistory`-Modell

**Files:**
- Modify: `apps/module_firmware/models.py`
- Test: `tests/module_firmware/test_assignment_history.py`

**Interfaces:**
- Produces: `ModuleAssignmentHistory(module: FK Module CASCADE, station: FK stations.Station SET_NULL null, slot: str, from_ts, to_ts null, reason: str, created_by: FK User SET_NULL null)`. Zwei partielle Unique-Constraints auf offene Zeilen.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_assignment_history.py
import pytest
from django.db import IntegrityError
from django.utils import timezone
from apps.module_firmware.models import Module, ModuleType, ModuleAssignmentHistory


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="U1", module_type=t)


@pytest.mark.django_db
def test_only_one_open_assignment_per_module(module, station_factory):
    station = station_factory()
    ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot1")
    with pytest.raises(IntegrityError):
        ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot2")


@pytest.mark.django_db
def test_closed_assignment_allows_new_open(module, station_factory):
    station = station_factory()
    old = ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot1")
    old.to_ts = timezone.now()
    old.save()
    ModuleAssignmentHistory.objects.create(module=module, station=station, slot="slot2")  # ok
```

> `station_factory` ist eine bestehende/zu ergänzende Fixture in `tests/conftest.py`. Falls nicht vorhanden: eine minimale Fixture ergänzen, die `Station.objects.create(name=...)` liefert (Feldpflicht aus `apps/stations/models.py` prüfen).

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Modell ergänzen** (`from_ts` default = jetzt via `auto_now_add`? Nein — explizit setzbar; default `timezone.now`)

```python
from django.conf import settings


class ModuleAssignmentHistory(models.Model):
    """Temporal module <-> (station, slot) assignment log. Open row = current."""

    module = models.ForeignKey(
        Module, verbose_name=_("module"), on_delete=models.CASCADE, related_name="assignments"
    )
    station = models.ForeignKey(
        "stations.Station", verbose_name=_("station"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name="module_assignments",
    )
    slot = models.CharField(_("slot"), max_length=64)
    from_ts = models.DateTimeField(_("from"), default=timezone.now)
    to_ts = models.DateTimeField(_("to"), null=True, blank=True)
    reason = models.CharField(_("reason"), max_length=64, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name=_("created by"),
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = _("module assignment")
        verbose_name_plural = _("module assignments")
        ordering = ["-from_ts"]
        constraints = [
            models.UniqueConstraint(
                fields=["module"], condition=models.Q(to_ts__isnull=True),
                name="uniq_open_assignment_per_module",
            ),
            models.UniqueConstraint(
                fields=["station", "slot"], condition=models.Q(to_ts__isnull=True),
                name="uniq_open_assignment_per_station_slot",
            ),
        ]

    def __str__(self):
        return f"{self.module.uid} @ {self.station_id}/{self.slot}"
```

Import `from django.utils import timezone` oben in `models.py` sicherstellen.

- [ ] **Step 4: Migration** — `python manage.py makemigrations module_firmware`

- [ ] **Step 5: Run** → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/module_firmware/models.py apps/module_firmware/migrations/ tests/module_firmware/test_assignment_history.py tests/conftest.py
git commit -m "feat(module_firmware): ModuleAssignmentHistory with partial-unique open-row constraints"
```

---

## Task 4: `StationModule.tracked_module`-FK

**Files:**
- Modify: `apps/control/models.py`
- Test: `tests/module_firmware/test_stationmodule_link.py`

**Interfaces:**
- Produces: `StationModule.tracked_module` (nullable FK → `module_firmware.Module`, `SET_NULL`, `related_name="station_modules"`).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_stationmodule_link.py
import pytest
from apps.control.models import StationModule
from apps.module_firmware.models import Module, ModuleType


@pytest.mark.django_db
def test_stationmodule_links_to_module(station_factory):
    station = station_factory()
    t = ModuleType.objects.create(key="fm", display_name="FM")
    mod = Module.objects.create(uid="U1", module_type=t)
    sm = StationModule.objects.create(station=station, slot="slot1", module_id="fm", module=mod)
    assert sm.tracked_module == mod
    assert mod.station_modules.first() == sm
```

- [ ] **Step 2: Run** → FAIL (`module` unbekannt).

- [ ] **Step 3: FK ergänzen** — in `apps/control/models.py` bei `StationModule`, nach `version`:

> **Feldname `tracked_module` (nicht `module`):** `StationModule` hat bereits ein
> `module_id`-CharField; ein FK `module` würde auf derselben `module_id`-Spalte/
> `attname` kollidieren. Attribut daher `tracked_module`, FK-`_id`-Spalte
> `tracked_module_id`, `related_name` bleibt `station_modules`.

```python
    tracked_module = models.ForeignKey(
        "module_firmware.Module",
        verbose_name=_("module"),
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="station_modules",
    )
```

- [ ] **Step 4: Migration** — `python manage.py makemigrations control`

- [ ] **Step 5: Run** → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control/models.py apps/control/migrations/ tests/module_firmware/test_stationmodule_link.py
git commit -m "feat(control): link StationModule to tracked Module (nullable FK)"
```

---

## Task 5: `StationAuditLog`-Erweiterung (Modul als Subjekt)

**Files:**
- Modify: `apps/stations/models.py` (StationAuditLog: `module`-FK, `station` nullable, neue EventTypes, `log()`-Signatur)
- Test: `tests/module_firmware/test_audit_module_subject.py`

**Interfaces:**
- Produces: `StationAuditLog.module` (nullable FK → `module_firmware.Module`, `SET_NULL`, `related_name="audit_logs"`); `station` jetzt `null=True`; neue `EventType.MODULE_DISCOVERED/MODULE_REGISTERED/MODULE_SWAPPED/MODULE_LIFECYCLE_CHANGED/MODULE_ASSIGNMENT_CHANGED`. `StationAuditLog.log(...)` akzeptiert zusätzlich `module=None`, `module_id=None`; erlaubt Aufruf **ohne** station, sofern ein module/module_id gegeben ist.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_audit_module_subject.py
import pytest
from apps.stations.models import StationAuditLog
from apps.module_firmware.models import Module, ModuleType


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="U1", module_type=t)


@pytest.mark.django_db
def test_audit_can_log_module_without_station(module):
    entry = StationAuditLog.log(
        module=module,
        event_type=StationAuditLog.EventType.MODULE_DISCOVERED,
        message="discovered",
    )
    assert entry.module == module
    assert entry.station is None
    assert module.audit_logs.count() == 1


@pytest.mark.django_db
def test_audit_dual_subject(module, station_factory):
    station = station_factory()
    StationAuditLog.log(
        station=station, module=module,
        event_type=StationAuditLog.EventType.MODULE_SWAPPED, message="swap",
    )
    assert station.audit_logs.filter(module=module).count() == 1
    assert module.audit_logs.filter(station=station).count() == 1
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: EventTypes ergänzen** — in `StationAuditLog.EventType` (nach `CONTROL_PTT`):

```python
        MODULE_DISCOVERED = "module_discovered", _("Module Discovered")
        MODULE_REGISTERED = "module_registered", _("Module Registered")
        MODULE_SWAPPED = "module_swapped", _("Module Swapped")
        MODULE_LIFECYCLE_CHANGED = "module_lifecycle_changed", _("Module Lifecycle Changed")
        MODULE_ASSIGNMENT_CHANGED = "module_assignment_changed", _("Module Assignment Changed")
```

- [ ] **Step 4: `station` nullable + `module`-FK** — `StationAuditLog.station` auf `null=True, blank=True` setzen; neues Feld:

```python
    station = models.ForeignKey(
        Station, verbose_name=_("station"), on_delete=models.CASCADE,
        related_name="audit_logs", null=True, blank=True,
    )
    module = models.ForeignKey(
        "module_firmware.Module", verbose_name=_("module"), on_delete=models.SET_NULL,
        related_name="audit_logs", null=True, blank=True,
    )
```

- [ ] **Step 5: `__str__` absichern** (station kann None sein)

```python
    def __str__(self):
        subject = self.station.name if self.station_id else (self.module.uid if self.module_id else "—")
        return f"{subject} - {self.get_event_type_display()} - {self.created_at}"
```

- [ ] **Step 6: `log()`-Signatur erweitern** — `module=None, module_id=None` ergänzen; Validierung: mindestens eins von station/station_id/module/module_id; module/module_id in kwargs durchreichen. Ersetze die Guard-Logik:

```python
    @classmethod
    def log(cls, station=None, event_type=None, message="", changes=None,
            user=None, ip_address=None, station_id=None, module=None, module_id=None):
        if station is None and station_id is None and module is None and module_id is None:
            raise ValueError("a station or module subject is required")
        if station is not None and station_id is not None:
            raise ValueError("pass either station or station_id, not both")
        if module is not None and module_id is not None:
            raise ValueError("pass either module or module_id, not both")
        if not event_type:
            raise ValueError("event_type is required")
        kwargs = {"event_type": event_type, "message": message,
                  "changes": changes or {}, "user": user, "ip_address": ip_address}
        if station is not None:
            kwargs["station"] = station
        elif station_id is not None:
            kwargs["station_id"] = station_id
        if module is not None:
            kwargs["module"] = module
        elif module_id is not None:
            kwargs["module_id"] = module_id
        return cls.objects.create(**kwargs)
```

- [ ] **Step 7: Index für Modul-Sicht** — in `StationAuditLog.Meta.indexes` ergänzen: `models.Index(fields=["module", "-created_at"])`.

- [ ] **Step 8: Migration** — `python manage.py makemigrations stations`

- [ ] **Step 9: Run** — `pytest tests/module_firmware/test_audit_module_subject.py -v` → PASS. Zusätzlich bestehende Audit-Tests laufen lassen: `pytest -k audit` → PASS (Regression: `station`-nullable darf nichts brechen).

- [ ] **Step 10: Commit**

```bash
git add apps/stations/models.py apps/stations/migrations/ tests/module_firmware/test_audit_module_subject.py
git commit -m "feat(stations): make Module an audit subject (nullable module FK, station nullable, module events)"
```

---

## Task 6: Ingestion-Kern — `ingest_module` (create/update, Typ-Auflösung, No-UID/Unknown-Type)

**Files:**
- Create: `apps/module_firmware/ingest.py`
- Test: `tests/module_firmware/test_ingest_core.py`

**Interfaces:**
- Produces: `ingest_module(station, slot, module_id, identity, *, now, user=None) -> Module | None`. `identity` ist das self-reported Dict (`type, model, version, uid?, uid_source?`). Rückgabe `None` wenn keine UID (Legacy) oder unbekannter Typ. Bei bekannter/neuer UID: `Module` erstellt/aktualisiert (`last_seen=now`, `last_reported_version=identity["version"]`), neu ⇒ `first_seen=now`, `registration_status=unregistered`, Audit `MODULE_DISCOVERED`. Assignment/Swap/Lifecycle folgen in Task 7/8 (hier noch nicht).
- Consumes: `Module`, `ModuleType`, `StationAuditLog.log`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_ingest_core.py
import pytest
from django.utils import timezone
from apps.module_firmware.models import Module, ModuleType
from apps.module_firmware.ingest import ingest_module
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(**kw):
    base = {"type": "fm", "model": "SA818", "version": "1.0.0"}
    base.update(kw)
    return base


@pytest.mark.django_db
def test_no_uid_returns_none_and_creates_no_module(fm, station_factory):
    station = station_factory()
    result = ingest_module(station, "slot1", "fm", ident(), now=timezone.now())
    assert result is None
    assert Module.objects.count() == 0


@pytest.mark.django_db
def test_unknown_type_rejected(station_factory):
    station = station_factory()
    result = ingest_module(station, "slot1", "power", ident(type="power", uid="X1"), now=timezone.now())
    assert result is None
    assert Module.objects.count() == 0


@pytest.mark.django_db
def test_new_uid_creates_unregistered_module_and_audits(fm, station_factory):
    station = station_factory()
    now = timezone.now()
    m = ingest_module(station, "slot1", "fm", ident(uid="ABC"), now=now)
    assert m.uid == "ABC"
    assert m.registration_status == Module.Registration.UNREGISTERED
    assert m.first_seen == now and m.last_seen == now
    assert m.last_reported_version == "1.0.0"
    assert StationAuditLog.objects.filter(
        module=m, event_type=StationAuditLog.EventType.MODULE_DISCOVERED).count() == 1


@pytest.mark.django_db
def test_known_uid_updates_only_seen_and_version(fm, station_factory):
    station = station_factory()
    first = timezone.now()
    m1 = ingest_module(station, "slot1", "fm", ident(uid="ABC"), now=first)
    later = first + timezone.timedelta(minutes=5)
    m2 = ingest_module(station, "slot1", "fm", ident(uid="ABC", version="1.1.0"), now=later)
    assert m1.pk == m2.pk
    assert m2.first_seen == first and m2.last_seen == later
    assert m2.last_reported_version == "1.1.0"
    # only one DISCOVERED audit
    assert StationAuditLog.objects.filter(
        module=m2, event_type=StationAuditLog.EventType.MODULE_DISCOVERED).count() == 1
    assert Module.objects.count() == 1
```

- [ ] **Step 2: Run** → FAIL (`ingest` fehlt).

- [ ] **Step 3: `apps/module_firmware/ingest.py`**

```python
"""Module inventory ingestion. Pure ORM, synchronous — called from
apps.control.registry.apply_inventory inside its atomic transaction.

Assignment/swap and lifecycle derivation live in _apply_assignment /
_derive_lifecycle (added in later tasks); ingest_module wires them in.
"""

import logging

from apps.stations.models import StationAuditLog

from .models import Module, ModuleType

logger = logging.getLogger(__name__)


def ingest_module(station, slot, module_id, identity, *, now, user=None):
    """Upsert a Module from a self-reported identity dict. Returns the Module
    or None (no UID => legacy path; unknown type => rejected)."""
    identity = identity or {}
    uid = identity.get("uid")
    if not uid:
        return None  # legacy firmware without UID: StationModule-only display

    type_key = identity.get("type") or module_id
    try:
        module_type = ModuleType.objects.get(key=type_key)
    except ModuleType.DoesNotExist:
        StationAuditLog.log(
            station=station,
            event_type=StationAuditLog.EventType.UPDATED,
            message=f"Ignored module with unregistered type '{type_key}' (uid={uid}).",
        )
        logger.warning("ingest: unknown module type %r (uid=%s)", type_key, uid)
        return None

    uid_source = identity.get("uid_source") or Module.UidSource.STM32_UID
    version = identity.get("version", "")

    module, created = Module.objects.get_or_create(
        uid=uid,
        defaults={
            "module_type": module_type,
            "uid_source": uid_source,
            "last_reported_version": version,
            "first_seen": now,
            "last_seen": now,
        },
    )
    if created:
        StationAuditLog.log(
            station=station, module=module,
            event_type=StationAuditLog.EventType.MODULE_DISCOVERED,
            message=f"Module {uid} ({module_type.key}) discovered in {station}/{slot}.",
        )
    else:
        module.last_seen = now
        module.last_reported_version = version
        module.save(update_fields=["last_seen", "last_reported_version", "updated_at"])

    # Assignment/swap + lifecycle derivation are wired in Task 7/8.
    return module
```

- [ ] **Step 4: Run** — `pytest tests/module_firmware/test_ingest_core.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/ingest.py tests/module_firmware/test_ingest_core.py
git commit -m "feat(module_firmware): ingest_module core (create/update, type resolve, no-uid/unknown-type)"
```

---

## Task 7: Ingestion — Assignment + Swap-Erkennung

**Files:**
- Modify: `apps/module_firmware/ingest.py`
- Test: `tests/module_firmware/test_ingest_swap.py`

**Interfaces:**
- Produces: interne `_apply_assignment(module, station, slot, *, now, user) -> bool` (True wenn sich die Zuordnung geändert hat); von `ingest_module` **vor** dem Return aufgerufen. Effekt: schließt konkurrierende offene Assignments (dasselbe Modul woanders offen; derselbe (station,slot) mit anderem Modul offen), öffnet eine neue offene Zeile, auditiert `MODULE_SWAPPED` (nur wenn ein Vorgänger verdrängt wurde) + `MODULE_ASSIGNMENT_CHANGED`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_ingest_swap.py
import pytest
from django.utils import timezone
from apps.module_firmware.models import Module, ModuleType, ModuleAssignmentHistory
from apps.module_firmware.ingest import ingest_module
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(uid, v="1.0.0"):
    return {"type": "fm", "model": "SA818", "version": v, "uid": uid}


@pytest.mark.django_db
def test_first_seen_opens_assignment(fm, station_factory):
    station = station_factory()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=timezone.now())
    open_rows = ModuleAssignmentHistory.objects.filter(module=m, to_ts__isnull=True)
    assert open_rows.count() == 1
    assert open_rows.first().station == station and open_rows.first().slot == "slot1"


@pytest.mark.django_db
def test_idempotent_same_slot_no_new_rows(fm, station_factory):
    station = station_factory()
    now = timezone.now()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=now)
    ingest_module(station, "slot1", "fm", ident("A", v="1.1.0"), now=now + timezone.timedelta(minutes=1))
    assert ModuleAssignmentHistory.objects.filter(module=m).count() == 1
    assert StationAuditLog.objects.filter(event_type=StationAuditLog.EventType.MODULE_SWAPPED).count() == 0


@pytest.mark.django_db
def test_new_uid_in_slot_swaps(fm, station_factory):
    station = station_factory()
    t0 = timezone.now()
    a = ingest_module(station, "slot1", "fm", ident("A"), now=t0)
    t1 = t0 + timezone.timedelta(hours=1)
    b = ingest_module(station, "slot1", "fm", ident("B"), now=t1)
    # A's assignment closed, B's opened
    a_row = ModuleAssignmentHistory.objects.get(module=a)
    assert a_row.to_ts == t1
    assert ModuleAssignmentHistory.objects.filter(module=b, to_ts__isnull=True).count() == 1
    assert StationAuditLog.objects.filter(
        module=b, event_type=StationAuditLog.EventType.MODULE_SWAPPED).count() == 1


@pytest.mark.django_db
def test_module_moved_to_new_slot_closes_old(fm, station_factory):
    station = station_factory()
    t0 = timezone.now()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=t0)
    t1 = t0 + timezone.timedelta(hours=1)
    ingest_module(station, "slot2", "fm", ident("A"), now=t1)
    rows = ModuleAssignmentHistory.objects.filter(module=m).order_by("from_ts")
    assert rows.count() == 2
    assert rows[0].slot == "slot1" and rows[0].to_ts == t1
    assert rows[1].slot == "slot2" and rows[1].to_ts is None
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: `_apply_assignment` ergänzen + in `ingest_module` einhängen**

In `ingest.py` importieren: `from .models import Module, ModuleType, ModuleAssignmentHistory`. Vor `return module` in `ingest_module`:

```python
    _apply_assignment(module, station, slot, now=now, user=user)
    return module
```

Neue Funktion:

```python
def _apply_assignment(module, station, slot, *, now, user=None):
    """Ensure exactly one open assignment for (module) at (station, slot).
    Closes any conflicting open rows and opens a new one on change.
    Returns True if the assignment changed."""
    current = module.assignments.filter(to_ts__isnull=True).first()
    if current and current.station_id == station.id and current.slot == slot:
        return False  # unchanged — idempotent

    displaced = False

    # Close this module's open assignment elsewhere.
    if current:
        current.to_ts = now
        current.save(update_fields=["to_ts"])

    # Close whatever other module currently occupies (station, slot).
    occupant = ModuleAssignmentHistory.objects.filter(
        station=station, slot=slot, to_ts__isnull=True
    ).exclude(module=module).first()
    if occupant:
        occupant.to_ts = now
        occupant.save(update_fields=["to_ts"])
        displaced = True

    ModuleAssignmentHistory.objects.create(
        module=module, station=station, slot=slot,
        from_ts=now, reason="auto-swap", created_by=user,
    )

    if displaced:
        StationAuditLog.log(
            station=station, module=module,
            event_type=StationAuditLog.EventType.MODULE_SWAPPED,
            message=f"Module {module.uid} replaced a module in {station}/{slot}.",
        )
    StationAuditLog.log(
        station=station, module=module,
        event_type=StationAuditLog.EventType.MODULE_ASSIGNMENT_CHANGED,
        message=f"Module {module.uid} assigned to {station}/{slot}.",
    )
    return True
```

- [ ] **Step 4: Run** — `pytest tests/module_firmware/test_ingest_swap.py -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/ingest.py tests/module_firmware/test_ingest_swap.py
git commit -m "feat(module_firmware): assignment history + swap detection in ingestion"
```

---

## Task 8: Ingestion — Lifecycle-Auto-Ableitung

**Files:**
- Modify: `apps/module_firmware/ingest.py`
- Test: `tests/module_firmware/test_ingest_lifecycle.py`

**Interfaces:**
- Produces: interne `_derive_lifecycle(module, *, now) -> None`; von `ingest_module` **nach** `_apply_assignment` aufgerufen. Regel: hat das Modul eine offene Assignment ⇒ `deployed`; sonst ⇒ `ready`; sticky (`STICKY_LIFECYCLE`) nie überschreiben. Bei Änderung Audit `MODULE_LIFECYCLE_CHANGED`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_ingest_lifecycle.py
import pytest
from django.utils import timezone
from apps.module_firmware.models import Module, ModuleType
from apps.module_firmware.ingest import ingest_module
from apps.stations.models import StationAuditLog


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def ident(uid):
    return {"type": "fm", "version": "1.0.0", "uid": uid}


@pytest.mark.django_db
def test_active_assignment_sets_deployed(fm, station_factory):
    m = ingest_module(station_factory(), "slot1", "fm", ident("A"), now=timezone.now())
    m.refresh_from_db()
    assert m.lifecycle_status == Module.Lifecycle.DEPLOYED
    assert StationAuditLog.objects.filter(
        module=m, event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED).count() == 1


@pytest.mark.django_db
def test_sticky_states_not_overridden(fm, station_factory):
    m = ingest_module(station_factory(), "slot1", "fm", ident("A"), now=timezone.now())
    m.lifecycle_status = Module.Lifecycle.DEFECT
    m.save()
    # re-ingest at same slot: assignment unchanged, but even if derived, defect stays
    ingest_module(m.station_modules.first().station, "slot1", "fm", ident("A"), now=timezone.now())
    m.refresh_from_db()
    assert m.lifecycle_status == Module.Lifecycle.DEFECT
```

> Falls `m.station_modules` in diesem Ingestion-only-Test leer ist (StationModule wird erst in Task 9 verlinkt), stattdessen die Station aus der Fixture wiederverwenden: Test so schreiben, dass die `station` als lokale Variable gehalten wird.

Korrigierte zweite Testhälfte:

```python
@pytest.mark.django_db
def test_sticky_states_not_overridden(fm, station_factory):
    station = station_factory()
    m = ingest_module(station, "slot1", "fm", ident("A"), now=timezone.now())
    m.lifecycle_status = Module.Lifecycle.DEFECT
    m.save(update_fields=["lifecycle_status"])
    ingest_module(station, "slot1", "fm", ident("A"), now=timezone.now())
    m.refresh_from_db()
    assert m.lifecycle_status == Module.Lifecycle.DEFECT
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: `_derive_lifecycle` ergänzen + einhängen**

In `ingest_module`, nach `_apply_assignment(...)`:

```python
    _derive_lifecycle(module, now=now)
    return module
```

```python
def _derive_lifecycle(module, *, now):
    if module.lifecycle_status in Module.STICKY_LIFECYCLE:
        return
    has_open = module.assignments.filter(to_ts__isnull=True).exists()
    target = Module.Lifecycle.DEPLOYED if has_open else Module.Lifecycle.READY
    if module.lifecycle_status != target:
        old = module.lifecycle_status
        module.lifecycle_status = target
        module.save(update_fields=["lifecycle_status", "updated_at"])
        StationAuditLog.log(
            module=module,
            event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED,
            message=f"Lifecycle {old} → {target} for module {module.uid}.",
            changes={"lifecycle_status": {"old": old, "new": target}},
        )
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/module_firmware/ingest.py tests/module_firmware/test_ingest_lifecycle.py
git commit -m "feat(module_firmware): lifecycle auto-derivation (deployed/ready, sticky states preserved)"
```

---

## Task 9: Ingestion in `apply_inventory` verdrahten

**Files:**
- Modify: `apps/control/registry.py`
- Test: `tests/module_firmware/test_apply_inventory_integration.py`

**Interfaces:**
- Consumes: `ingest_module(station, slot, module_id, identity, *, now)`.
- Produces: `apply_inventory` setzt `StationModule.tracked_module` auf das ingestete `Module` (oder lässt es `None` bei Legacy/unknown).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_apply_inventory_integration.py
import pytest
from apps.control.registry import apply_inventory
from apps.control.models import StationModule
from apps.module_firmware.models import Module, ModuleType, ModuleAssignmentHistory


@pytest.fixture
def fm(db):
    return ModuleType.objects.create(key="fm", display_name="FM")


def slots(uid=None, version="1.0.0"):
    identity = {"type": "fm", "model": "SA818", "version": version}
    if uid:
        identity["uid"] = uid
    return [{"slot": "slot1", "control": "/dev/x",
             "modules": [{"module": "fm", "identity": identity, "capabilities": [], "state": {}}]}]


@pytest.mark.django_db
def test_apply_inventory_links_module(fm, station_factory):
    station = station_factory()
    apply_inventory(station, slots(uid="ABC"))
    sm = StationModule.objects.get(station=station, slot="slot1", module_id="fm")
    assert sm.tracked_module is not None and sm.tracked_module.uid == "ABC"
    assert Module.objects.count() == 1
    assert ModuleAssignmentHistory.objects.filter(module=sm.tracked_module, to_ts__isnull=True).count() == 1


@pytest.mark.django_db
def test_apply_inventory_legacy_no_uid_leaves_module_null(fm, station_factory):
    station = station_factory()
    apply_inventory(station, slots(uid=None))
    sm = StationModule.objects.get(station=station, slot="slot1", module_id="fm")
    assert sm.tracked_module is None
    assert Module.objects.count() == 0
```

- [ ] **Step 2: Run** → FAIL (kein Module verlinkt).

- [ ] **Step 3: `apply_inventory` erweitern** — in `apps/control/registry.py` den Import ergänzen und nach dem `update_or_create` das Modul ingesten + verlinken:

```python
from apps.module_firmware.ingest import ingest_module
```

Innerhalb der Modul-Schleife, den `update_or_create`-Block so anpassen, dass die Instanz gehalten wird, dann ingesten:

```python
            sm, _ = StationModule.objects.update_or_create(
                station=station, slot=slot, module_id=module_id,
                defaults={
                    "type": identity.get("type", ""),
                    "model": identity.get("model", ""),
                    "version": identity.get("version", ""),
                    "capability_descriptor": cap_descriptor,
                    "last_state": filtered_state,
                    "online": True,
                    "last_seen": now,
                },
            )
            tracked = ingest_module(station, slot, module_id, identity, now=now)
            if sm.tracked_module_id != (tracked.id if tracked else None):
                sm.tracked_module = tracked
                sm.save(update_fields=["module"])
            reported.append((slot, module_id))
```

> Achtung Namenskollision: `module_id` ist die bestehende *StationModule*-CharField-Spalte (Firmware-Modul-ID). Der neue FK heißt daher `tracked_module`; seine FK-`_id`-Spalte ist `tracked_module_id`. Der Vergleich `sm.tracked_module_id != tracked.id` nutzt die FK-`_id`-Spalte — korrekt, da `sm.tracked_module_id` nach `update_or_create` die aktuelle FK hält.

- [ ] **Step 4: Run** — `pytest tests/module_firmware/test_apply_inventory_integration.py -v` → PASS. Regression: `pytest tests/ -k "control or registry"` → PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/control/registry.py tests/module_firmware/test_apply_inventory_integration.py
git commit -m "feat(control): wire Module ingestion into apply_inventory + link StationModule.tracked_module"
```

---

## Task 10: Registrierungs-/Lifecycle-Services + Admin

**Files:**
- Create: `apps/module_firmware/services.py`, `apps/module_firmware/admin.py`
- Test: `tests/module_firmware/test_services.py`

**Interfaces:**
- Produces: `services.confirm_registration(module, *, user) -> None` (setzt `registered`, Audit `MODULE_REGISTERED`; idempotent); `services.set_lifecycle(module, status, *, user) -> None` (setzt Lifecycle inkl. sticky, Audit `MODULE_LIFECYCLE_CHANGED`). Admin: `ModuleType`, `Module` (uid/type/uid_source readonly; bulk action „confirm registration"), `ModuleAssignmentHistory` (readonly).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_services.py
import pytest
from django.contrib.auth import get_user_model
from apps.module_firmware.models import Module, ModuleType
from apps.module_firmware import services
from apps.stations.models import StationAuditLog

User = get_user_model()


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="U1", module_type=t)


@pytest.mark.django_db
def test_confirm_registration(module):
    user = User.objects.create_user(username="admin", password="x", is_staff=True)
    services.confirm_registration(module, user=user)
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.REGISTERED
    assert StationAuditLog.objects.filter(
        module=module, event_type=StationAuditLog.EventType.MODULE_REGISTERED, user=user).count() == 1
    # idempotent
    services.confirm_registration(module, user=user)
    assert StationAuditLog.objects.filter(
        module=module, event_type=StationAuditLog.EventType.MODULE_REGISTERED).count() == 1


@pytest.mark.django_db
def test_set_lifecycle_defect(module):
    services.set_lifecycle(module, Module.Lifecycle.DEFECT, user=None)
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.DEFECT
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: `services.py`**

```python
from apps.stations.models import StationAuditLog

from .models import Module


def confirm_registration(module, *, user):
    if module.registration_status == Module.Registration.REGISTERED:
        return
    module.registration_status = Module.Registration.REGISTERED
    module.save(update_fields=["registration_status", "updated_at"])
    StationAuditLog.log(
        module=module, user=user,
        event_type=StationAuditLog.EventType.MODULE_REGISTERED,
        message=f"Module {module.uid} registration confirmed.",
    )


def set_lifecycle(module, status, *, user):
    if module.lifecycle_status == status:
        return
    old = module.lifecycle_status
    module.lifecycle_status = status
    module.save(update_fields=["lifecycle_status", "updated_at"])
    StationAuditLog.log(
        module=module, user=user,
        event_type=StationAuditLog.EventType.MODULE_LIFECYCLE_CHANGED,
        message=f"Lifecycle {old} → {status} for module {module.uid}.",
        changes={"lifecycle_status": {"old": old, "new": status}},
    )
```

- [ ] **Step 4: `admin.py`**

```python
from django.contrib import admin

from . import services
from .models import Module, ModuleAssignmentHistory, ModuleType


@admin.register(ModuleType)
class ModuleTypeAdmin(admin.ModelAdmin):
    list_display = ("key", "display_name", "hw_repo")
    search_fields = ("key", "display_name")


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("uid", "module_type", "lifecycle_status", "registration_status",
                    "last_reported_version", "last_seen")
    list_filter = ("module_type", "lifecycle_status", "registration_status", "uid_source")
    search_fields = ("uid",)
    readonly_fields = ("uid", "module_type", "uid_source", "first_seen", "last_seen",
                       "last_reported_version", "created_at", "updated_at")
    actions = ["confirm_registration"]

    @admin.action(description="Registrierung bestätigen")
    def confirm_registration(self, request, queryset):
        for module in queryset:
            services.confirm_registration(module, user=request.user)


@admin.register(ModuleAssignmentHistory)
class ModuleAssignmentHistoryAdmin(admin.ModelAdmin):
    list_display = ("module", "station", "slot", "from_ts", "to_ts", "reason")
    list_filter = ("reason",)
    search_fields = ("module__uid",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
```

- [ ] **Step 5: Run** — `pytest tests/module_firmware/test_services.py -v` → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/module_firmware/services.py apps/module_firmware/admin.py tests/module_firmware/test_services.py
git commit -m "feat(module_firmware): registration/lifecycle services + Django admin"
```

---

## Task 11: `fake_fw` — synthetische UID (Sim-Testbarkeit)

**Files:**
- Modify: `tests/fake_fw.py`
- Test: `tests/module_firmware/test_fake_fw_uid.py`

**Interfaces:**
- Produces: die `fake_fw`-DESCRIBE-`identity` enthält `uid` (stabil pro Modul-Spec) + `uid_source="synthetic"`. Bestehende `make_slot_tree`/Modul-Spec-API bleibt kompatibel.

- [ ] **Step 1: Bestehende fake_fw-Modul-Spec-Struktur lesen** — in `tests/fake_fw.py` nachsehen, wie `self._modules` (Mapping id→spec) initialisiert wird und welche `identity` die Spec bisher trägt. Ziel: `identity` um `uid`/`uid_source` erweitern, ohne den Aufruf-Vertrag zu brechen.

- [ ] **Step 2: Failing test**

```python
# tests/module_firmware/test_fake_fw_uid.py
import pytest
from tests.fake_fw import make_slot_tree  # bestehende Helper-API
from station_agent.slot_discovery import discover_slots


@pytest.mark.django_db
def test_fake_fw_reports_synthetic_uid(tmp_path):
    # Baue einen Sim-Slot mit einem fm-Modul, das eine synthetische UID meldet.
    base = make_slot_tree(tmp_path, {1: {"fm": {"identity": {
        "type": "fm", "model": "sim", "version": "0.0.1",
        "uid": "SIM-FM-0001", "uid_source": "synthetic"}, "capabilities": []}}})
    entries = discover_slots(base, timeout=2.0)
    identity = entries[0]["modules"][0]["identity"]
    assert identity["uid"] == "SIM-FM-0001"
    assert identity["uid_source"] == "synthetic"
```

> Die exakte Signatur von `make_slot_tree`/der Modul-Spec ist in Step 1 zu verifizieren; den Test an die reale Spec-Shape anpassen (die obige Form ist die erwartete Struktur laut `tests/fake_fw.py`-Docstring). Kern-Assertion bleibt: synthetische UID kommt durch `discover_slots` durch.

- [ ] **Step 3: Run** → FAIL falls die Default-Spec keine uid trägt / bzw. Test rot bis fake_fw die identity durchreicht.

- [ ] **Step 4: `fake_fw` anpassen** — sicherstellen, dass die pro-Modul-Spec (`self._modules[mid]`) im `MODULE-DESCRIBE`-JSON die `identity` inkl. `uid`/`uid_source` unverändert ausliefert (die DESCRIBE-Zeile dumpt `spec` bereits als JSON — es genügt, dass die Test-Spec diese Felder trägt und `make_slot_tree` sie durchreicht). Falls `make_slot_tree` heute nur `capabilities`/`type` durchreicht: die Helper so erweitern, dass eine vollständige `identity` (inkl. `uid`, `uid_source`) übernommen wird.

- [ ] **Step 5: Run** → PASS. Zusätzlich bestehende fake_fw/broker-E2E-Tests: `pytest tests/ -k "fake_fw or broker or slot"` → PASS (keine Regression im Discovery-Vertrag).

- [ ] **Step 6: Commit**

```bash
git add tests/fake_fw.py tests/module_firmware/test_fake_fw_uid.py
git commit -m "test(fake_fw): emit synthetic persisted UID + uid_source for sim ingestion tests"
```

---

## Task 12: Modul-Inventarliste (`/modules/`)

> **REQUIRED:** `Skill("frontend-design")` vor UI-Arbeit invoken.

**Files:**
- Create: `apps/module_firmware/views.py`, `apps/module_firmware/urls.py`, `apps/module_firmware/templates/module_firmware/module_list.html`
- Modify: `config/urls.py`
- Test: `tests/module_firmware/test_views_list.py`

**Interfaces:**
- Produces: `ModuleListView` (LoginRequired, read-all) unter `path("", ...)` von `apps.module_firmware.urls`, included unter `modules/` in `config/urls.py`, `app_name = "module_firmware"`, URL-Name `module_list`. Filter-Query-Params: `type`, `lifecycle`, `registration`, `uid_source`, `station`, `q` (uid-Suche).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_views_list.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.fixture
def data(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    Module.objects.create(uid="REAL1", module_type=t)
    Module.objects.create(uid="SIM1", module_type=t, uid_source=Module.UidSource.SYNTHETIC)


@pytest.mark.django_db
def test_list_requires_login(client, data):
    resp = client.get(reverse("module_firmware:module_list"))
    assert resp.status_code in (302, 403)


@pytest.mark.django_db
def test_list_visible_to_any_user_and_filters(client, data):
    user = User.objects.create_user(username="u", password="x")
    client.force_login(user)
    resp = client.get(reverse("module_firmware:module_list"))
    assert resp.status_code == 200
    assert b"REAL1" in resp.content and b"SIM1" in resp.content
    resp = client.get(reverse("module_firmware:module_list") + "?uid_source=stm32_uid")
    assert b"REAL1" in resp.content and b"SIM1" not in resp.content
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: `views.py` (ListView)**

```python
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.views.generic import ListView

from .models import Module


class ModuleListView(LoginRequiredMixin, ListView):
    model = Module
    template_name = "module_firmware/module_list.html"
    context_object_name = "modules"
    paginate_by = 50

    def get_queryset(self):
        qs = Module.objects.select_related("module_type").prefetch_related("station_modules__station")
        g = self.request.GET
        if g.get("type"):
            qs = qs.filter(module_type__key=g["type"])
        if g.get("lifecycle"):
            qs = qs.filter(lifecycle_status=g["lifecycle"])
        if g.get("registration"):
            qs = qs.filter(registration_status=g["registration"])
        if g.get("uid_source"):
            qs = qs.filter(uid_source=g["uid_source"])
        if g.get("station"):
            qs = qs.filter(station_modules__station_id=g["station"]).distinct()
        if g.get("q"):
            qs = qs.filter(Q(uid__icontains=g["q"]))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["lifecycles"] = Module.Lifecycle.choices
        ctx["registrations"] = Module.Registration.choices
        ctx["unregistered_count"] = Module.objects.filter(
            registration_status=Module.Registration.UNREGISTERED).count()
        return ctx
```

- [ ] **Step 4: `urls.py`**

```python
from django.urls import path

from . import views

app_name = "module_firmware"

urlpatterns = [
    path("", views.ModuleListView.as_view(), name="module_list"),
]
```

- [ ] **Step 5: `config/urls.py`** — nach den anderen App-Includes: `path("modules/", include("apps.module_firmware.urls")),`

- [ ] **Step 6: Template** `templates/module_firmware/module_list.html` — Basis-Template des Projekts erweitern (Muster aus `apps/stations` oder `apps/images` templates übernehmen: `{% extends %}`, Bootstrap-Table, Badges). Spalten laut Spec (UID mono, Typ, Lifecycle-/Registration-/uid_source-Badge „SIM", Standort, last_seen, Version). Filter-Leiste (GET-Form). „Unregistered"-Tab/Filter mit `unregistered_count`-Badge. **Kein** Multi-line `{# #}`. Frontend-design-Skill für Layout/Aesthetik.

- [ ] **Step 7: Run** — `pytest tests/module_firmware/test_views_list.py -v` → PASS.

- [ ] **Step 8: Commit**

```bash
git add apps/module_firmware/views.py apps/module_firmware/urls.py apps/module_firmware/templates/ config/urls.py tests/module_firmware/test_views_list.py
git commit -m "feat(module_firmware): fleet module inventory list view + filters"
```

---

## Task 13: Modul-Detail (`/modules/<uid>/`) — Lebensgeschichte

> **REQUIRED:** `Skill("frontend-design")`.

**Files:**
- Modify: `apps/module_firmware/views.py`, `apps/module_firmware/urls.py`
- Create: `apps/module_firmware/templates/module_firmware/module_detail.html`
- Test: `tests/module_firmware/test_views_detail.py`

**Interfaces:**
- Produces: `ModuleDetailView` (LoginRequired), URL-Name `module_detail`, `slug_field="uid"`, `slug_url_kwarg="uid"`. Kontext: `assignments` (nach `-from_ts`), `audit_logs` (module-zentrisch), `can_edit` (`request.user.is_staff`).

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_views_detail.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from apps.module_firmware.models import Module, ModuleType, ModuleAssignmentHistory
from apps.stations.models import StationAuditLog

User = get_user_model()


@pytest.fixture
def module(db, station_factory):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    m = Module.objects.create(uid="LIFE1", module_type=t)
    ModuleAssignmentHistory.objects.create(module=m, station=station_factory(), slot="slot1")
    StationAuditLog.log(module=m, event_type=StationAuditLog.EventType.MODULE_DISCOVERED, message="x")
    return m


@pytest.mark.django_db
def test_detail_shows_history_and_audit(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.status_code == 200
    assert b"LIFE1" in resp.content
    assert b"slot1" in resp.content


@pytest.mark.django_db
def test_detail_edit_controls_only_for_staff(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.context["can_edit"] is False
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.get(reverse("module_firmware:module_detail", args=[module.uid]))
    assert resp.context["can_edit"] is True
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: `ModuleDetailView` in `views.py`**

```python
from django.views.generic import DetailView


class ModuleDetailView(LoginRequiredMixin, DetailView):
    model = Module
    template_name = "module_firmware/module_detail.html"
    context_object_name = "module"
    slug_field = "uid"
    slug_url_kwarg = "uid"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.object
        ctx["assignments"] = m.assignments.select_related("station").order_by("-from_ts")
        ctx["audit_logs"] = m.audit_logs.select_related("station", "user").all()[:200]
        ctx["can_edit"] = self.request.user.is_staff
        ctx["lifecycles"] = Module.Lifecycle.choices
        return ctx
```

- [ ] **Step 4: URL** — in `urls.py`: `path("<str:uid>/", views.ModuleDetailView.as_view(), name="module_detail"),`

- [ ] **Step 5: Template** `module_detail.html` — Header (UID kopierbar, Typ, uid_source, Lifecycle-/Registration-Badge, Standort), Assignment-Timeline, Firmware-Sektion (`last_reported_version` + Versions-Changes aus Audit — ehrlich dünn), modul-zentrischer Audit-Trail. Edit-Controls nur wenn `can_edit`. Frontend-design-Skill.

- [ ] **Step 6: Run** → PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/module_firmware/views.py apps/module_firmware/urls.py apps/module_firmware/templates/ tests/module_firmware/test_views_detail.py
git commit -m "feat(module_firmware): module detail life-story view (history + module audit)"
```

---

## Task 14: Detail-Mutationen (Confirm / Lifecycle / Notes) — staff-only

> **REQUIRED:** `Skill("frontend-design")` für die Formular-Controls.

**Files:**
- Modify: `apps/module_firmware/views.py`, `apps/module_firmware/urls.py`, `module_detail.html`
- Test: `tests/module_firmware/test_views_mutations.py`

**Interfaces:**
- Produces: POST-Endpoints `module_confirm` (`<uid>/confirm/`), `module_lifecycle` (`<uid>/lifecycle/`), `module_notes` (`<uid>/notes/`) — alle `is_staff`-gated (403 sonst), rufen `services.confirm_registration` / `services.set_lifecycle` / setzen `notes`, danach Redirect auf `module_detail`.

- [ ] **Step 1: Failing test**

```python
# tests/module_firmware/test_views_mutations.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.fixture
def module(db):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    return Module.objects.create(uid="M1", module_type=t)


@pytest.mark.django_db
def test_non_staff_cannot_confirm(client, module):
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.post(reverse("module_firmware:module_confirm", args=[module.uid]))
    assert resp.status_code == 403
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.UNREGISTERED


@pytest.mark.django_db
def test_staff_confirms(client, module):
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(reverse("module_firmware:module_confirm", args=[module.uid]))
    assert resp.status_code == 302
    module.refresh_from_db()
    assert module.registration_status == Module.Registration.REGISTERED


@pytest.mark.django_db
def test_staff_sets_lifecycle(client, module):
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True))
    resp = client.post(reverse("module_firmware:module_lifecycle", args=[module.uid]),
                       {"lifecycle_status": "defect"})
    assert resp.status_code == 302
    module.refresh_from_db()
    assert module.lifecycle_status == Module.Lifecycle.DEFECT
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Mutations-Views** (`views.py`) — ein `StaffRequiredMixin` (via `UserPassesTestMixin`) + drei `View`s oder Funktions-Views:

```python
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View

from . import services


class _StaffModuleMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        return self.request.user.is_staff

    def get_module(self):
        return get_object_or_404(Module, uid=self.kwargs["uid"])

    def _redirect(self, uid):
        return HttpResponseRedirect(reverse("module_firmware:module_detail", args=[uid]))


class ModuleConfirmView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        services.confirm_registration(m, user=request.user)
        return self._redirect(uid)


class ModuleLifecycleView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        status = request.POST.get("lifecycle_status")
        if status in dict(Module.Lifecycle.choices):
            services.set_lifecycle(m, status, user=request.user)
        return self._redirect(uid)


class ModuleNotesView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        m.notes = request.POST.get("notes", "")
        m.save(update_fields=["notes", "updated_at"])
        return self._redirect(uid)
```

> `UserPassesTestMixin` liefert bei nicht-bestandenem Test standardmäßig 403, wenn `raise_exception=True`. Setze `raise_exception = True` im Mixin, damit der Test (403 statt Redirect-zu-Login) grün wird.

Ergänze im Mixin: `raise_exception = True`.

- [ ] **Step 4: URLs** — in `urls.py`:

```python
    path("<str:uid>/confirm/", views.ModuleConfirmView.as_view(), name="module_confirm"),
    path("<str:uid>/lifecycle/", views.ModuleLifecycleView.as_view(), name="module_lifecycle"),
    path("<str:uid>/notes/", views.ModuleNotesView.as_view(), name="module_notes"),
```

- [ ] **Step 5: Template-Controls** — im `module_detail.html` innerhalb `{% if can_edit %}` drei kleine POST-Forms (CSRF!) für Confirm / Lifecycle-Select / Notes.

- [ ] **Step 6: Run** → PASS.

- [ ] **Step 7: Commit**

```bash
git add apps/module_firmware/views.py apps/module_firmware/urls.py apps/module_firmware/templates/ tests/module_firmware/test_views_mutations.py
git commit -m "feat(module_firmware): staff-only confirm/lifecycle/notes mutations on module detail"
```

---

## Task 15: Stations-Detail-Integration (Link + Badges)

> **REQUIRED:** `Skill("frontend-design")`.

**Files:**
- Modify: das Control-/Station-Modul-Panel-Template (in `apps/control/templates/…` bzw. `apps/stations/templates/…`; in Step 1 lokalisieren) + ggf. `apps/control/views.py`
- Test: `tests/module_firmware/test_station_integration.py`

**Interfaces:**
- Consumes: `StationModule.tracked_module` (Task 4/9). Produces: pro belegtem Slot mit verknüpftem `Module` ein Link auf `module_firmware:module_detail` + UID/Registration/Lifecycle-Badge.

- [ ] **Step 1: Panel-Template lokalisieren** — `grep -rn "modules" apps/control/templates apps/stations/templates` und die Stelle finden, wo `station.modules` / `ctx["modules"]` gerendert werden (`apps/control/views.py:33` liefert `ctx["modules"]`).

- [ ] **Step 2: Failing test**

```python
# tests/module_firmware/test_station_integration.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.control.models import StationModule
from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.mark.django_db
def test_station_page_links_to_module_detail(client, station_factory):
    station = station_factory()
    t = ModuleType.objects.create(key="fm", display_name="FM")
    mod = Module.objects.create(uid="LINK1", module_type=t)
    StationModule.objects.create(station=station, slot="slot1", module_id="fm",
                                 type="fm", module=mod, online=True)
    # login (station detail requires auth); use staff to avoid topology scoping in test
    client.force_login(User.objects.create_user(username="s", password="x", is_staff=True, is_superuser=True))
    # Station-Detail-URL aus apps/stations bzw. apps/control ermitteln:
    url = reverse("stations:detail", args=[station.pk])  # ggf. anpassen an realen URL-Namen
    resp = client.get(url)
    assert resp.status_code == 200
    assert reverse("module_firmware:module_detail", args=["LINK1"]).encode() in resp.content
```

> Der Stations-Detail-URL-Name (`stations:detail` o. ä.) ist in Step 1 zu verifizieren und im Test einzusetzen.

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: Template anpassen** — im Modul-Panel pro Slot: wenn `module.module` (der FK) gesetzt ist, UID als Link `{% url 'module_firmware:module_detail' module.module.uid %}` + Badges für `module.module.get_registration_status_display` / `get_lifecycle_status_display`. `{% comment %}`-Syntax für Kommentare.

- [ ] **Step 5: Run** → PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/control/ apps/stations/ tests/module_firmware/test_station_integration.py
git commit -m "feat(control): link station module panel slots to tracked Module detail"
```

---

## Task 16: Dashboard-Card

> **REQUIRED:** `Skill("frontend-design")`.

**Files:**
- Modify: `apps/dashboard/views.py` + Dashboard-Template
- Test: `tests/module_firmware/test_dashboard_card.py`

**Interfaces:**
- Produces: Dashboard-Kontext `module_stats = {"total", "unregistered", "attention"}` (attention = defect+in_lab); Card mit Link auf `module_firmware:module_list` (+ gefilterte Links).

- [ ] **Step 1: Dashboard-View lokalisieren** — `apps/dashboard/views.py` lesen; die Haupt-Dashboard-View + Template finden, Muster der bestehenden Cards übernehmen.

- [ ] **Step 2: Failing test**

```python
# tests/module_firmware/test_dashboard_card.py
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from apps.module_firmware.models import Module, ModuleType

User = get_user_model()


@pytest.mark.django_db
def test_dashboard_shows_module_counts(client):
    t = ModuleType.objects.create(key="fm", display_name="FM")
    Module.objects.create(uid="A", module_type=t)  # unregistered
    Module.objects.create(uid="B", module_type=t, lifecycle_status=Module.Lifecycle.DEFECT)
    client.force_login(User.objects.create_user(username="u", password="x"))
    resp = client.get(reverse("dashboard:index"))  # realen Namen in Step 1 verifizieren
    assert resp.status_code == 200
    assert resp.context["module_stats"]["total"] == 2
    assert resp.context["module_stats"]["unregistered"] == 2
    assert resp.context["module_stats"]["attention"] == 1
```

- [ ] **Step 3: Run** → FAIL.

- [ ] **Step 4: View-Kontext ergänzen** — in der Dashboard-View:

```python
from apps.module_firmware.models import Module

# in get_context_data:
ctx["module_stats"] = {
    "total": Module.objects.count(),
    "unregistered": Module.objects.filter(
        registration_status=Module.Registration.UNREGISTERED).count(),
    "attention": Module.objects.filter(
        lifecycle_status__in=[Module.Lifecycle.DEFECT, Module.Lifecycle.IN_LAB]).count(),
}
```

- [ ] **Step 5: Card im Dashboard-Template** — „Module: N gesamt · X unregistered · Y Aufmerksamkeit", verlinkt auf `module_firmware:module_list` (+ `?registration=unregistered`). Frontend-design-Skill.

- [ ] **Step 6: Run** → PASS.

- [ ] **Step 7: Volltest + Commit**

```bash
pytest tests/module_firmware/ -v
git add apps/dashboard/ tests/module_firmware/test_dashboard_card.py
git commit -m "feat(dashboard): module inventory summary card"
```

---

## Abschluss

- [ ] **Gesamter Testlauf:** `pytest` (grün) + `python manage.py makemigrations --check --dry-run` (keine ausstehenden Migrationen).
- [ ] **Manuelle Sim-Verifikation** (optional, empfohlen): mit `fake_fw`-Slot-Tree eine synthetische UID durch `apply_inventory` schicken und Liste/Detail im UI prüfen.
- [ ] Übergabe an Kontor-Kind-Session zur Ausführung (Manager-Muster): PR → CI grün → Copilot-Loop bis 0, dann „done".

## Self-Review-Notiz (bereits durchgeführt)

- **Spec-Abdeckung:** Datenmodell (T1–T5), Ingestion/Lifecycle/Swap/Idempotenz (T6–T9), Sim (T11), Services/Admin (T10), volles A.4-UI (T12–T16), Audit-Dual-Subjekt (T5 + über alle Ingestion-Tasks). Graceful-No-UID (T6/T9), Onboarding-ii (T6), Sichtbarkeit read-all/edit-staff (T12–T14). Keine offenen Spec-Punkte ohne Task.
- **Type-Konsistenz:** `ingest_module(station, slot, module_id, identity, *, now, user=None)` einheitlich in T6–T9; `services.confirm_registration/set_lifecycle` T10/T14; Choices-Namen (`Lifecycle`/`Registration`/`UidSource`) durchgängig; `StationAuditLog.log(module=…)` ab T5 verfügbar und ab T6 genutzt.
- **Verifikationspflichten:** URL-Namen (`stations:detail`, `dashboard:index`) und `fake_fw`-Spec-Shape sind explizit als „in Step 1 verifizieren" markiert, da sie aus dem Bestand gelesen werden müssen.
