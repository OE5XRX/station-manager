# Remove Legacy firmware/builder/module Cluster — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vollständig entfernen: die Django-Apps `apps.firmware` und `apps.builder` sowie `stations.ModuleType` + `Station.installed_modules` — inkl. sauberem Drop der (leeren) Tabellen auf der Prod-DB.

**Architecture:** Code-Entfernung in `stations`, Komplett-Löschung der Apps `firmware`+`builder`, History-Scrub der `deployments`-Migrationen (sie hängen historisch an `firmware`, nutzen aber längst `images.ImageRelease`), plus eine idempotente RunSQL-Cleanup-Migration in `stations`, die orphaned Tabellen + `django_migrations`-Zeilen auf Prod beseitigt. Reihenfolge ist kritisch (FK `firmware_firmwareartifact.target_module → stations_moduletype`): firmware-Tabellen müssen vor `ModuleType` gedroppt werden.

**Tech Stack:** Django 6.0, Python 3.14, PostgreSQL 17 (Prod), SQLite (Test-Settings), pytest (`config.settings.test`).

## Global Constraints

- **Pi-Image-/OTA-Pipeline ist tabu:** `apps.images` (`ImageRelease`), `apps.deployments` (Model + 0003), `apps.rollouts`, `apps.provisioning` dürfen funktional NICHT verändert werden. Einzige erlaubte deployments-Änderung: History-Scrub der `firmware`-Referenz in `0001`/`0002` (Body von bereits angewendeten Migrationen; läuft auf Prod nicht erneut).
- **Verifiziert leer in Prod (2026-06-21):** alle Counts (ModuleType, installed_modules, FirmwareArtifact, FirmwareDelta, BuildConfig, BuildJob) = 0. Keine Datenmigration nötig, nur Schema-Drop.
- **Prod-Migrationen laufen automatisch** über den `init`-Container (`manage.py migrate --noinput`) beim Deploy. Alle neuen Migrationen müssen idempotent + ohne manuelle DB-Schritte durchlaufen („keine manuellen VM-Änderungen").
- **Django-Template-Comment-Regel:** niemals multi-line `{# … #}` — immer `{% comment %}…{% endcomment %}`.
- **Test-Settings nutzen SQLite** — die destruktive FK-Reihenfolge wird dort NICHT geprüft. Pflicht-Gate: Postgres-Prod-Upgrade-Simulation (Task 8).
- **Squash-Merge:** ein Commit pro PR auf `main` (Konvention station-manager). Innerhalb des Branches trotzdem pro Task committen.

---

## File Structure

**Gelöscht (ganze Verzeichnisse):**
- `apps/firmware/` (models, views, urls, forms, admin, apps, delta.py, management/commands/compute_deltas.py, templates/firmware/*)
- `apps/builder/` (models, views, urls, forms, admin, apps, templates/builder/*)
- `tests/test_firmware.py`

**Modifiziert:**
- `apps/stations/models.py` — `ModuleType` + `installed_modules` entfernen
- `apps/stations/forms.py` — `installed_modules` aus `StationForm` (fields + widgets)
- `apps/stations/admin.py` — `ModuleType`-Import, `ModuleTypeAdmin`, `filter_horizontal`, Hardware-Fieldset
- `apps/stations/views.py` — `installed_modules` aus `prefetch_related`
- `apps/stations/templates/stations/station_detail.html` — Modules-Tab (Button + Panel)
- `apps/stations/templates/stations/station_form.html` — `installed_modules`-Feld
- `apps/dashboard/views.py` — `firmware_count` + Import
- `apps/dashboard/templates/dashboard/index.html` — Firmware-Stat-Card
- `templates/includes/sidebar.html` — Firmware- + Builder-Nav-Links
- `config/settings/base.py` — `apps.firmware`, `apps.builder` aus INSTALLED_APPS; Kommentar Zeile ~162
- `config/urls.py` — `firmware/` + `builder/` includes
- `apps/deployments/migrations/0001_initial.py` — `firmware`-Dependency + `firmware_artifact`-Feld entfernen (History-Scrub)
- `apps/deployments/migrations/0002_swap_to_image_release.py` — `RemoveField(firmware_artifact)` entfernen (History-Scrub)
- `tests/conftest.py` — `firmware_artifact`-Fixture + `FirmwareArtifact`-Import entfernen

**Neu erstellt:**
- `apps/stations/migrations/0013_drop_legacy_firmware_builder_tables.py` — idempotente RunSQL (Prod-Cleanup)
- `apps/stations/migrations/0014_remove_moduletype_installed_modules.py` — via `makemigrations` generiert

**Reihenfolge-Begründung:** Erst Code raus (Tasks 1–4), dann deployments-History-Scrub (Task 5), dann Migrationen generieren (Task 6) — `makemigrations` lädt den Graphen nur fehlerfrei, wenn keine `firmware`-Referenzen mehr da sind. Task 7 Tests, Task 8 Verifikation.

---

### Task 1: `stations.ModuleType` + `installed_modules` aus dem Code entfernen

**Files:**
- Modify: `apps/stations/models.py`
- Modify: `apps/stations/forms.py`
- Modify: `apps/stations/admin.py`
- Modify: `apps/stations/views.py`
- Modify: `apps/stations/templates/stations/station_detail.html`
- Modify: `apps/stations/templates/stations/station_form.html`

**Interfaces:**
- Produces: keine `ModuleType`/`installed_modules`-Referenzen mehr im `stations`-Code (Voraussetzung für `makemigrations` in Task 6).

- [ ] **Step 1: `ModuleType`-Klasse aus `models.py` löschen**

Entferne in `apps/stations/models.py` den kompletten Block (die Klasse inkl. `FlashMethod`):

```python
class ModuleType(models.Model):
    """Types of hardware modules (e.g., FM Transceiver, Power Board)."""

    class FlashMethod(models.TextChoices):
        USB_DFU = "usb_dfu", _("USB DFU")
        UART = "uart", _("UART")
        SPI = "spi", _("SPI")
        OTHER = "other", _("Other")

    name = models.CharField(_("name"), max_length=100, unique=True)
    slug = models.SlugField(_("slug"), unique=True)
    description = models.TextField(_("description"), blank=True)
    firmware_flash_method = models.CharField(
        _("firmware flash method"),
        max_length=10,
        choices=FlashMethod.choices,
        default=FlashMethod.OTHER,
    )

    class Meta:
        verbose_name = _("module type")
        verbose_name_plural = _("module types")
        ordering = ["name"]

    def __str__(self):
        return self.name
```

- [ ] **Step 2: `installed_modules`-Feld aus `Station` löschen**

Entferne in `apps/stations/models.py` im `Station`-Model:

```python
    installed_modules = models.ManyToManyField(
        ModuleType,
        verbose_name=_("installed modules"),
        blank=True,
        related_name="stations",
    )
```

- [ ] **Step 3: `forms.py` bereinigen**

In `apps/stations/forms.py` aus `StationForm.Meta.fields` die Zeile `"installed_modules",` entfernen und aus `widgets` die Zeile `"installed_modules": forms.CheckboxSelectMultiple(),` entfernen.

- [ ] **Step 4: `admin.py` bereinigen**

In `apps/stations/admin.py`:
- Im Import-Block `ModuleType,` entfernen.
- `filter_horizontal = ("installed_modules", "tags")` → `filter_horizontal = ("tags",)`.
- Im `_("Hardware")`-Fieldset die Zeile `"installed_modules",` entfernen (nur `"hardware_revision",` bleibt).
- Den kompletten `ModuleTypeAdmin`-Block löschen:

```python
@admin.register(ModuleType)
class ModuleTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "firmware_flash_method")
    prepopulated_fields = {"slug": ("name",)}
    search_fields = ("name",)
```

- [ ] **Step 5: `views.py` prefetch bereinigen**

In `apps/stations/views.py` ändern:

```python
            .prefetch_related("tags", "installed_modules", "photos", "log_entries", "audit_logs")
```
zu:
```python
            .prefetch_related("tags", "photos", "log_entries", "audit_logs")
```

- [ ] **Step 6: `station_detail.html` Modules-Tab entfernen**

In `apps/stations/templates/stations/station_detail.html`:
- Tab-Button (Zeile ~92) löschen:
```html
      <button type="button" class="tab" data-tab="modules" aria-selected="false">{% trans "Modules" %} <span class="count">{{ station.installed_modules.count }}</span></button>
```
- Das zugehörige Modules-Panel (Block um Zeile ~271–~290, der `{% if station.installed_modules.all %}` … `{% endif %}` enthält, inkl. umschließendem Tab-Panel-`<div>`) vollständig entfernen.

- [ ] **Step 7: `station_form.html` Feld entfernen**

In `apps/stations/templates/stations/station_form.html` den `installed_modules`-Block (Zeile ~85, `{{ form.installed_modules }}` inkl. zugehörigem Label/Wrapper-Markup) entfernen.

- [ ] **Step 8: Prüfen, dass keine `installed_modules`/`ModuleType`-Reste in `stations`/Templates bleiben**

Run:
```bash
grep -rnE "installed_modules|ModuleType" apps/stations templates 2>/dev/null
```
Expected: keine Treffer.

- [ ] **Step 9: Commit**

```bash
git add apps/stations templates
git commit -m "refactor(stations): remove legacy ModuleType + installed_modules usage"
```

---

### Task 2: App `apps.builder` löschen

**Files:**
- Delete: `apps/builder/` (gesamtes Verzeichnis)

**Interfaces:**
- Consumes: nichts (keine andere App importiert `builder`-Symbole — verifiziert).
- Produces: keine `builder`-URLs/-Views mehr.

- [ ] **Step 1: Verzeichnis löschen**

```bash
git rm -r apps/builder
```

- [ ] **Step 2: Prüfen, dass nur noch INSTALLED_APPS/urls/nav auf builder zeigen (in späteren Tasks behandelt)**

Run:
```bash
grep -rnE "apps\.builder|'builder'|\"builder\"|builder:" --include='*.py' --include='*.html' . | grep -v '/.worktrees/'
```
Expected: nur noch Treffer in `config/settings/base.py`, `config/urls.py`, `templates/includes/sidebar.html`.

- [ ] **Step 3: Commit**

```bash
git commit -m "refactor: delete legacy builder app code"
```

---

### Task 3: App `apps.firmware` löschen + Dashboard entkoppeln

**Files:**
- Delete: `apps/firmware/` (gesamtes Verzeichnis)
- Modify: `apps/dashboard/views.py`
- Modify: `apps/dashboard/templates/dashboard/index.html`

**Interfaces:**
- Consumes: nichts mehr aus `firmware` außerhalb von `dashboard` (deployments nutzt `images`).
- Produces: Dashboard ohne `firmware_count`.

- [ ] **Step 1: `dashboard/views.py` entkoppeln**

In `apps/dashboard/views.py`:
- Import entfernen: `from apps.firmware.models import FirmwareArtifact`
- Zeile entfernen: `context["firmware_count"] = FirmwareArtifact.objects.count()`

- [ ] **Step 2: Firmware-Stat-Card aus `index.html` entfernen**

In `apps/dashboard/templates/dashboard/index.html` den kompletten Block entfernen:

```html
    <div class="stat">
      <div class="stat-label">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h8"/></svg>
        {% trans "Firmware" %}
      </div>
      <div class="stat-value">{{ firmware_count }}</div>
      <div class="stat-meta"><a href="{% url 'firmware:firmware_list' %}" class="t-mono">{% trans "artifacts" %} →</a></div>
    </div>
```

- [ ] **Step 3: firmware-App-Verzeichnis löschen**

```bash
git rm -r apps/firmware
```

- [ ] **Step 4: Prüfen, dass nur noch INSTALLED_APPS/urls/nav/migrations-deps auf firmware zeigen**

Run:
```bash
grep -rnE "apps\.firmware|'firmware'|\"firmware\"|firmware:|FirmwareArtifact|FirmwareDelta" --include='*.py' --include='*.html' . | grep -v '/.worktrees/'
```
Expected: nur noch `config/settings/base.py`, `config/urls.py`, `templates/includes/sidebar.html`, `apps/deployments/migrations/0001_initial.py`, `apps/deployments/migrations/0002_swap_to_image_release.py`, `tests/conftest.py`, `tests/test_firmware.py`.

- [ ] **Step 5: Commit**

```bash
git add apps/dashboard
git commit -m "refactor: delete legacy firmware app + dashboard firmware_count"
```

---

### Task 4: INSTALLED_APPS, URLConf, Sidebar-Nav bereinigen

**Files:**
- Modify: `config/settings/base.py`
- Modify: `config/urls.py`
- Modify: `templates/includes/sidebar.html`

**Interfaces:**
- Produces: `firmware`/`builder` sind nicht mehr installiert/route-bar/verlinkt. (Migrations-Graph noch inkonsistent bis Task 5 — `manage.py` Befehle erst danach grün.)

- [ ] **Step 1: INSTALLED_APPS bereinigen**

In `config/settings/base.py` die Zeilen `    "apps.firmware",` und `    "apps.builder",` entfernen.

- [ ] **Step 2: Media-Kommentar anpassen**

In `config/settings/base.py` Zeile ~162 `# Media files (firmware artifacts, station photos)` → `# Media files (station photos)`.

- [ ] **Step 3: URLConf bereinigen**

In `config/urls.py` die Zeilen entfernen:
```python
    path("firmware/", include("apps.firmware.urls")),
    path("builder/", include("apps.builder.urls")),
```

- [ ] **Step 4: Sidebar-Nav bereinigen**

In `templates/includes/sidebar.html` den Firmware-`<a>`-Block (`{% url 'firmware:firmware_list' %}` … `</a>`) und den Builder-`<a>`-Block (`{% url 'builder:buildconfig_list' %}` … `</a>`) entfernen. Den `Deployments`-Link und die `Fleet Updates`-Section-Überschrift behalten.

- [ ] **Step 5: Commit**

```bash
git add config templates
git commit -m "refactor: drop firmware/builder from INSTALLED_APPS, urls, nav"
```

---

### Task 5: `deployments`-Migrationshistorie von `firmware` entkoppeln (History-Scrub)

**Files:**
- Modify: `apps/deployments/migrations/0001_initial.py`
- Modify: `apps/deployments/migrations/0002_swap_to_image_release.py`

**Interfaces:**
- Produces: Migrations-Graph ohne `firmware`-Knoten → `manage.py` / `makemigrations` laden wieder fehlerfrei.

**Hintergrund:** Prod hat `0001`/`0002` längst angewendet; Django re-runt Migrationen nach Namen, nicht nach Inhalt → Body-Edit läuft auf Prod NICHT erneut. Auf frischer DB muss der Endzustand identisch bleiben (`image_release` vorhanden, kein `firmware_artifact`).

- [ ] **Step 1: `0001_initial.py` — firmware-Dependency entfernen**

In `apps/deployments/migrations/0001_initial.py` aus `dependencies` die Zeile entfernen:
```python
        ('firmware', '0002_initial'),
```

- [ ] **Step 2: `0001_initial.py` — `firmware_artifact`-Feld entfernen**

Im `CreateModel(name='Deployment', …)` die Feldzeile entfernen:
```python
                ('firmware_artifact', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='deployments', to='firmware.firmwareartifact', verbose_name='firmware artifact')),
```

- [ ] **Step 3: `0002_swap_to_image_release.py` — RemoveField entfernen**

In `apps/deployments/migrations/0002_swap_to_image_release.py` die Operation entfernen (das Feld wird in `0001` jetzt gar nicht mehr erzeugt):
```python
        migrations.RemoveField(
            model_name="deployment",
            name="firmware_artifact",
        ),
```
`AddField(image_release)` und `AlterField(deploymentresult.status)` bleiben unverändert.

- [ ] **Step 4: Migrations-Graph lädt wieder — verifizieren**

Run:
```bash
python manage.py makemigrations --check --dry-run --settings=config.settings.test
```
Expected: läuft ohne `NodeNotFoundError`; meldet die noch fehlende `stations`-Migration (für die in Task 1 entfernten Modelle) als „missing" — das ist erwartet und wird in Task 6 erzeugt.

- [ ] **Step 5: Commit**

```bash
git add apps/deployments/migrations
git commit -m "refactor(deployments): scrub firmware references from migration history"
```

---

### Task 6: Drop-Migrationen erzeugen (RunSQL-Cleanup + ModuleType-Removal)

**Files:**
- Create: `apps/stations/migrations/0013_drop_legacy_firmware_builder_tables.py`
- Create: `apps/stations/migrations/0014_remove_moduletype_installed_modules.py` (via `makemigrations`)

**Interfaces:**
- Consumes: bereinigter Code aus Tasks 1–5.
- Produces: zwei `stations`-Migrationen; `0013` läuft vor `0014` (FK-Reihenfolge).

- [ ] **Step 1: Leere RunSQL-Migration anlegen**

```bash
python manage.py makemigrations stations --empty --name drop_legacy_firmware_builder_tables --settings=config.settings.test
```
Expected: erstellt `apps/stations/migrations/0013_drop_legacy_firmware_builder_tables.py` mit `dependencies = [("stations", "0012_extend_station_audit_event_types")]`.

- [ ] **Step 2: RunSQL-Inhalt einfügen**

Ersetze `operations = []` in `0013_drop_legacy_firmware_builder_tables.py` durch:

```python
    operations = [
        # Die firmware/builder-Tabellen sind in Prod verifiziert leer (2026-06-21)
        # und die Apps sind entfernt. Wir droppen die orphaned Tabellen + die
        # zugehörigen django_migrations-Zeilen idempotent. firmware vor stations_
        # moduletype-Drop (Task 0014), weil firmware_firmwareartifact.target_module
        # einen FK auf stations_moduletype hält. Auf frischer DB (Test/CI) sind die
        # Tabellen nie entstanden → IF EXISTS macht no-op.
        migrations.RunSQL(
            sql="""
            DROP TABLE IF EXISTS builder_buildjob CASCADE;
            DROP TABLE IF EXISTS builder_buildconfig CASCADE;
            DROP TABLE IF EXISTS firmware_firmwaredelta CASCADE;
            DROP TABLE IF EXISTS firmware_firmwareartifact CASCADE;
            DELETE FROM django_migrations WHERE app IN ('firmware', 'builder');
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
```

- [ ] **Step 3: ModuleType/installed_modules-Removal-Migration generieren**

```bash
python manage.py makemigrations stations --name remove_moduletype_installed_modules --settings=config.settings.test
```
Expected: erstellt `0014_remove_moduletype_installed_modules.py` mit `RemoveField(model_name='station', name='installed_modules')` + `DeleteModel(name='ModuleType')`, `dependencies = [("stations", "0013_drop_legacy_firmware_builder_tables")]`.

- [ ] **Step 4: Keine weiteren fehlenden Migrationen**

Run:
```bash
python manage.py makemigrations --check --dry-run --settings=config.settings.test
```
Expected: `No changes detected` (Exit 0).

- [ ] **Step 5: Commit**

```bash
git add apps/stations/migrations
git commit -m "feat(stations): drop legacy firmware/builder tables + ModuleType migrations"
```

---

### Task 7: Tests bereinigen

**Files:**
- Delete: `tests/test_firmware.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `firmware_artifact`-Fixture wird nur in `test_firmware.py` genutzt (verifiziert; `test_deployments.py:548` prüft nur `not hasattr` und braucht die Fixture nicht).

- [ ] **Step 1: firmware-Testdatei löschen**

```bash
git rm tests/test_firmware.py
```

- [ ] **Step 2: Fixture + Import aus `conftest.py` entfernen**

In `tests/conftest.py`:
- Import entfernen: `from apps.firmware.models import FirmwareArtifact`
- Die komplette `firmware_artifact`-Fixture entfernen:
```python
@pytest.fixture
def firmware_artifact(db, operator_user):
    """A FirmwareArtifact with a small dummy file."""
    dummy_file = SimpleUploadedFile(
        "firmware-test.bin",
        b"\x00\x01\x02\x03" * 64,
        content_type="application/octet-stream",
    )
    artifact = FirmwareArtifact(
        name="test-firmware",
        version="1.0.0",
        artifact_type=FirmwareArtifact.ArtifactType.OS_IMAGE,
        file=dummy_file,
        uploaded_by=operator_user,
    )
    artifact.save()
    return artifact
```
- Falls `SimpleUploadedFile` danach ungenutzt ist: dessen Import ebenfalls entfernen (vorher mit `grep -n SimpleUploadedFile tests/conftest.py` prüfen — nur entfernen, wenn keine weitere Nutzung).

- [ ] **Step 3: Commit**

```bash
git add tests
git commit -m "test: remove firmware test + fixture"
```

---

### Task 8: Verifikation (inkl. Postgres-Prod-Upgrade-Simulation)

**Files:** keine (reine Verifikation).

- [ ] **Step 1: System-Check + Migrations-Check**

Run:
```bash
python manage.py check --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
```
Expected: `System check identified no issues`; `No changes detected`.

- [ ] **Step 2: Frische DB migrieren (SQLite, Test-Settings)**

Run:
```bash
python manage.py migrate --settings=config.settings.test
```
Expected: alle Migrationen inkl. `stations.0013`/`0014` laufen fehlerfrei.

- [ ] **Step 3: Volle Testsuite**

Run:
```bash
pytest -q
```
Expected: alle Tests grün (insb. `tests/test_deployments.py`).

- [ ] **Step 4: Postgres-Prod-Upgrade-Simulation (Pflicht — SQLite deckt FK-Reihenfolge nicht ab)**

Simuliert „bestehende Prod-DB (alter Stand) → neuer Code migriert sauber":

```bash
# Wegwerf-Postgres 17 starten
docker run -d --rm --name sm-pgcheck -e POSTGRES_PASSWORD=x -e POSTGRES_DB=sm -e POSTGRES_USER=sm -p 55432:5432 postgres:17
# kurz warten bis ready
until docker exec sm-pgcheck pg_isready -U sm >/dev/null 2>&1; do sleep 1; done

export DJANGO_SETTINGS_MODULE=config.settings.prod
export DATABASE_URL="postgres://sm:x@localhost:55432/sm"   # bzw. die in prod.py erwarteten POSTGRES_* envs setzen

# 1) ALTEN Stand (origin/main) auf die DB migrieren
git stash --include-untracked || true
git switch --detach origin/main
python manage.py migrate
# 2) NEUEN Stand (dieser Branch) drüber migrieren — das ist der Prod-Deploy-Pfad
git switch chore/remove-legacy-firmware-builder-module-cluster
git stash pop || true
python manage.py migrate
# 3) Tabellen müssen weg sein
docker exec sm-pgcheck psql -U sm -d sm -c "\dt" | grep -E "firmware_|builder_|moduletype|installed_modules" && echo "FEHLER: Tabelle übrig" || echo "OK: alle Legacy-Tabellen weg"

docker stop sm-pgcheck
```
Expected: Schritt 2 läuft ohne FK-/Constraint-Fehler; Schritt 3 gibt `OK: alle Legacy-Tabellen weg`.

> Hinweis: Die exakten DB-Env-Variablen richten sich nach `config/settings/prod.py`. Falls `prod.py` einzelne `POSTGRES_*`-Variablen statt `DATABASE_URL` erwartet, diese entsprechend setzen. Andere Prod-Pflicht-Env-Vars (SECRET_KEY etc.) mit Dummy-Werten belegen, damit `migrate` startet.

- [ ] **Step 5: Repo-weiter Endabgleich**

Run:
```bash
grep -rnE "apps\.firmware|apps\.builder|FirmwareArtifact|FirmwareDelta|BuildConfig|BuildJob|ModuleType|installed_modules|firmware:|builder:" --include='*.py' --include='*.html' . | grep -v '/.worktrees/' | grep -v '/migrations/'
```
Expected: keine Treffer (Migrationen ausgenommen — der RunSQL-Drop nennt Tabellennamen bewusst).

---

## Self-Review

**Spec coverage:**
- Apps `firmware` + `builder` löschen → Tasks 2, 3, 4 (Code, INSTALLED_APPS, urls, nav, dashboard). ✓
- `ModuleType` + `installed_modules` löschen → Task 1 (Code) + Task 6 (Migration). ✓
- Pi-Image-Pipeline unangetastet → Global Constraints + nur History-Scrub an deployments (Task 5), Model/0003 unberührt. ✓
- Saubere Prod-DB (leere Tabellen droppen, ohne manuelle Schritte) → Task 6 RunSQL idempotent + Task 8 Postgres-Sim. ✓
- Migrations-Graph-Integrität (deployments/builder hingen an firmware) → Task 5 Scrub + Task 6 Reihenfolge. ✓

**Placeholder-Scan:** Keine TBD/TODO; alle Edits mit konkretem Code/Pfad; Migrationsinhalte ausgeschrieben. ✓

**Type/Namens-Konsistenz:** Migrationsnamen `0013_drop_legacy_firmware_builder_tables` / `0014_remove_moduletype_installed_modules` durchgängig; Tabellennamen `firmware_firmwareartifact`, `firmware_firmwaredelta`, `builder_buildconfig`, `builder_buildjob`, `stations_moduletype`, `stations_station_installed_modules` konsistent mit den 0001-Migrationen. ✓

**Risiko-Restpunkt:** `config/settings/prod.py` DB-Env-Form (DATABASE_URL vs. POSTGRES_*) ist in Task 8 als Hinweis markiert — der Implementierer liest `prod.py` und setzt die passenden Variablen.
