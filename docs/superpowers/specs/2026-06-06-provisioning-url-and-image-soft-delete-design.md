# Provisioning-URL Settings-Glue + ImageRelease Soft-Delete — Design

**Status:** Draft, brainstormed 2026-06-06.
**Ziel:** Zwei unzusammenhängende, kleine Patches in einem PR bündeln.

1. **Provisioning bäckt die richtige Server-URL ein.** Der CAX21→`oe5xrx-prod-01`-Migration (2026-05) ist die Settings-Glue für `SERVER_PUBLIC_URL` nie nachgezogen worden; jedes neu provisionierte Station-Image enthält stattdessen den hardcoded Fallback `https://ham.oe5xrx.org` (alter CAX21-Hostname). Alle frisch geflashten Stations rufen damit den falschen Server.
2. **`ImageRelease` bekommt Soft-Delete (Archive).** Hard-Delete ist nach PR #62 zwar nicht mehr 500-bug-anfällig, scheitert aber an `PROTECT`-FKs sobald irgendein `Deployment` oder `ProvisioningJob` das Release referenziert. Operatoren wollen alte Releases trotzdem aus der Liste schaffen ohne Audit-Trail zu zerstören — gleiche Motivation wie beim bestehenden `AppGrant.revoked_at` Pattern.

Beide Änderungen liegen im `images` / `provisioning` / `config` Umfeld, sind aber semantisch unabhängig und kriegen jeweils einen eigenen Commit auf dem gleichen Branch.

---

## Teil A — `SERVER_PUBLIC_URL` Settings-Glue

### A.1 Root-Cause (zur Erinnerung)

`servers/services/station_manager/docker-compose.yml:53` setzt die Env-Var auf den Container:

```yaml
SERVER_PUBLIC_URL: ${SERVER_PUBLIC_URL:-https://remote.oe5xrx.org}
```

`config/settings/base.py` liest sie **nie** in Django-Settings ein. Jede andere Env-Var (`SECRET_KEY`, `ALLOWED_HOSTS`, `USE_S3`, `EMAIL_HOST`, …) wird per `os.environ.get(...)` gemappt; nur `SERVER_PUBLIC_URL` fehlt.

`apps/provisioning/management/commands/run_background_jobs.py:218`:

```python
server_url = getattr(settings, "SERVER_PUBLIC_URL", "https://ham.oe5xrx.org")
```

`settings.SERVER_PUBLIC_URL` existiert nicht → `getattr` returnt den hardcoded Fallback `https://ham.oe5xrx.org` (alte CAX21-Domain). Dieser String landet via `config_render.render_config(server_url=…)` als `server_url:` in `/etc/stationagent/config.yml` und damit in jedem .wic, das seit der Migration provisioniert wurde.

### A.2 Fix

**Drei Änderungen:**

1. **`config/settings/base.py`** (neuer Block neben anderen Env-Var-Reads):

   ```python
   # Public base URL for stations to reach this server. Baked into the
   # station-agent's config.yml at provisioning time — see
   # apps/provisioning/management/commands/run_background_jobs.py and
   # apps/provisioning/config_render.py. Empty = provisioning fails loud
   # rather than poisoning new images with a stale URL.
   SERVER_PUBLIC_URL = os.environ.get("SERVER_PUBLIC_URL", "")
   ```

2. **`apps/provisioning/management/commands/run_background_jobs.py`** — ersetze den `getattr`-mit-Fallback durch fail-loud:

   ```python
   from django.core.exceptions import ImproperlyConfigured

   def _run_provisioning_job(job: ProvisioningJob) -> None:
       server_url = settings.SERVER_PUBLIC_URL
       if not server_url:
           raise ImproperlyConfigured(
               "SERVER_PUBLIC_URL must be set — provisioning bakes it "
               "into the station-agent config inside the rootfs. Empty "
               "value would silently produce non-functional images."
           )
       …
   ```

   Begründung **fail-loud statt sicherer Default**: Ein Default à la `https://remote.oe5xrx.org` würde exakt die gleiche Bug-Klasse beim nächsten Hostname-Wechsel wieder produzieren — jemand migriert, vergisst die Env-Var, alle neuen Stations zeigen still auf `remote.oe5xrx.org` obwohl der Dienst längst woanders liegt. Mit `ImproperlyConfigured` landet der `ProvisioningJob` in der bestehenden Exception-Behandlung (siehe `run_background_jobs.py` `_run_provisioning_job` Try/Except), schlägt fehl mit klarer Meldung, Operator sieht's sofort im UI.

3. **`tests/test_provisioning_server_url.py`** (neu) — zwei Test-Cases:
   - `test_provisioning_bakes_server_public_url_from_settings`: setzt `settings.SERVER_PUBLIC_URL` via `override_settings`, simuliert minimalen `ProvisioningJob`, mockt `guestfish.inject_provisioning_files` und `image_storage`, asserted der `config_yaml=`-Argumentwert enthält die gesetzte URL.
   - `test_provisioning_fails_loud_without_server_public_url`: leere `SERVER_PUBLIC_URL` → erwartet `ImproperlyConfigured` (oder zumindest: der Job landet in `FAILED` mit der erwarteten Error-Message im `error_message`-Feld, je nach wie weit oben die Exception von `_run_provisioning_job` gefangen wird).

### A.3 Operative Folge

- **Existierende Stations mit falscher Config heilen sich nicht selbst.** Der Station-Agent liest seine `server_url` aus der lokalen `/etc/stationagent/config.yml`, die beim Provisioning eingebrannt wurde. Operator muss diese Stations **re-provisionieren** (neuen Bundle generieren via UI, neu flashen). Der User hat angekündigt das händisch zu machen — kein Code-Schritt nötig.
- Nach dem Fix sind alle **neuen** Provisioning-Bundles korrekt; alte gehen einfach gegen `ham.oe5xrx.org` und failen leise solange Cloudflare-DNS dort nichts mehr Sinnvolles serviert.

---

## Teil B — `ImageRelease` Soft-Delete (Archive/Restore)

### B.1 Motivation

Der bestehende Hard-Delete (`apps/images/views.py:ImageDeleteView`, hardened in PR #62) wirft jetzt einen klaren Fehler wenn `Deployment` oder `ProvisioningJob` das Release referenzieren — operativ heißt das: ältere Releases sind **gar nicht mehr löschbar** sobald sie für irgendwas verwendet wurden, und es gibt keinen sinnvollen Mittelweg.

Soft-Delete (im Sinne von "Archive") gibt dem Operator den Befehl "raus aus der Liste, aber referentielle Integrität bleibt", parallel zu was `AppGrant.revoked_at` (`apps/sso/models.py:41`) für SSO-Grants leistet.

### B.2 Datenmodell-Änderungen

**`apps/images/models.py:ImageRelease`** bekommt:

```python
archived_at = models.DateTimeField(
    _("archived at"),
    null=True,
    blank=True,
    db_index=True,
    help_text=_(
        "Soft-delete timestamp. Archived releases are hidden from the "
        "default UI list but remain available for any Deployment or "
        "ProvisioningJob that still references them."
    ),
)
```

Plus:
- **Bestehende Constraints unverändert.** `UniqueConstraint(fields=["tag", "machine"], name="uniq_tag_per_machine")` bleibt full-unique — d.h. pro `(tag, machine)` existiert immer genau **eine** Row, entweder aktiv oder archiviert. Das macht den Auto-Restore-Pfad (siehe B.3) trivial: der bestehende `ImageRelease.objects.update_or_create(tag=..., machine=..., defaults={..., "archived_at": None})` Call im Import-Worker setzt `archived_at` atomar auf NULL zurück, ohne Constraint-Kollisionen. Eine partial-unique-Variante würde mehrere Rows pro Paar erlauben → `update_or_create` würde `MultipleObjectsReturned` werfen.
- **`uniq_latest_per_machine`** bleibt unverändert (partial unique auf `is_latest=True`). Beim Archivieren wird `is_latest` zwingend auf `False` gesetzt (siehe B.4) — ein "latest archived" wäre semantisch sinnlos.
- **Default-Ordering** bleibt `["-imported_at"]`.

**Manager-Pattern:**

```python
class ImageReleaseManager(models.Manager):
    """Default manager: hides archived rows.

    Use ``ImageRelease.all_objects`` to get the full set (incl. archived),
    e.g. for the "Show archived" toggle on the Image-Releases page or for
    auto-restore lookups during re-import.
    """
    def get_queryset(self):
        return super().get_queryset().filter(archived_at__isnull=True)

class ImageRelease(models.Model):
    …
    objects = ImageReleaseManager()
    all_objects = models.Manager()
```

Der `all_objects` Manager liefert das ungefilterte Set für gezielte Code-Stellen (Auto-Restore-Lookup, archivierte-Liste im UI, Django Admin).

Wichtige Konsequenz: Die FKs (`Deployment.image_release`, `ProvisioningJob.image_release`, `Station.current_image_release`) **referenzieren weiterhin korrekt** auf archivierte Rows — Django nimmt die unfiltered DB-Lookup-Funktion, nicht den Default-Manager, für FK-Resolves. Existierende Deployments/Provisionings funktionieren also unverändert.

Für `select_related("image_release")` und ähnliche Queries gilt: wenn die Quelle den Default-Manager benutzt, sieht sie das archivierte Release wieder nicht. Konkrete Audit:
- `apps/deployments/api_views.py:DeploymentCheckView` benutzt `select_related("deployment__image_release")` ausgehend von `DeploymentResult.objects.filter(...)`. Der Image-Release-FK wird per **PK-Lookup** aufgelöst und ignoriert den Default-Manager → Stationen kriegen das Release auch wenn's archiviert ist. ✓ OK.
- Image-List-View (`apps/images/views.py:ImageListView`) benutzt `ImageRelease`-Queryset direkt → soll archivierte filtern. ✓ Hier wirkt der Default-Manager wie gewünscht.

Wenn ein Code-Pfad doch explizit archivierte Rows braucht, nutzt er `ImageRelease.all_objects.…`.

### B.3 Auto-Restore beim Re-Import

User-Wahl: re-importierte (tag, machine) restored das archivierte Release.

**ImportJob-Worker `_run_import_job` in `apps/provisioning/management/commands/run_background_jobs.py:94`** (gleiche Datei wie der Provisioning-Worker — hier landet auch der GitHub-Pull) benutzt bereits `ImageRelease.objects.update_or_create(tag=..., machine=..., defaults={...})`. Auto-Restore ist ein One-Liner: `archived_at: None` in den `defaults`-Dict aufnehmen. Damit setzt der "update"-Pfad einen archivierten Eintrag atomar auf aktiv zurück; der "create"-Pfad legt neu an (default-NULL passt sowieso). Wenn vorhanden:
- Felder updaten: `s3_key`, `sha256`, `cosign_bundle_s3_key`, `size_bytes`, `rootfs_*`, `imported_at=timezone.now()`, `imported_by`
- `archived_at = None` (= Restore)
- `is_latest` nach `mark_as_latest`-Flag aus dem ImportForm setzen (gleiche Logik wie beim Neu-Anlegen)
- Save, fertig

Wenn kein archivierter Eintrag → normal anlegen wie bisher.

Das ist atomic, vermeidet die Unique-Constraint-Kollision die ohne Auto-Restore entstehen würde, und gibt dem Operator das erwartete Verhalten ("ich hab v1-alpha versehentlich archiviert, ich re-importiere kurz aus GitHub, alles ist wieder da").

### B.4 Archive-Mutation

Neue Model-Methode:

```python
def archive(self):
    """Soft-delete: timestamp + clear is_latest.

    Atomic so a concurrent reader never sees ``archived_at IS NOT NULL``
    with ``is_latest=True`` — the partial unique on is_latest plus the
    "latest archived" being nonsensical mean those two states must never
    coexist.
    """
    if self.archived_at is not None:
        return  # idempotent

    with transaction.atomic():
        self.archived_at = timezone.now()
        if self.is_latest:
            self.is_latest = False
        self.save(update_fields=["archived_at", "is_latest"])

def restore(self):
    """Undo archive. Sets archived_at = None, leaves is_latest alone.

    Re-promotion to latest is a separate operator action — restoring
    a previously archived release doesn't implicitly steal the latest
    bit from whatever is currently active.
    """
    if self.archived_at is None:
        return  # idempotent

    self.archived_at = None
    self.save(update_fields=["archived_at"])
```

### B.5 Views + URLs

**Neue Views in `apps/images/views.py`:**

```python
class ImageArchiveView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        release.archive()
        messages.success(
            request,
            _("Release %(tag)s archived.") % {"tag": release.tag},
        )
        return redirect("images:list")


class ImageRestoreView(AdminRequiredMixin, View):
    def post(self, request, pk):
        release = get_object_or_404(ImageRelease.all_objects, pk=pk)
        release.restore()
        messages.success(
            request,
            _("Release %(tag)s restored.") % {"tag": release.tag},
        )
        return redirect("images:list")
```

**Neue URLs in `apps/images/urls.py`:**

```python
path("<int:pk>/archive/", views.ImageArchiveView.as_view(), name="archive"),
path("<int:pk>/restore/", views.ImageRestoreView.as_view(), name="restore"),
```

**Bestehender `ImageDeleteView`/`images:delete` bleibt unverändert.** Nicht im UI verlinkt, aber via Django Admin und für gezielte Purge-Operationen weiterhin erreichbar.

### B.6 UI-Änderungen — `apps/images/templates/images/image_list.html`

Drei Block-Änderungen am Image-Releases-Template:

1. **Row-Action `Delete` → `Archive`** (Trash-Icon → Archive-Box-Icon, `data-confirm` neu formuliert):
   ```html
   <form method="post"
         action="{% url 'images:archive' rel.pk %}"
         style="display:inline;margin:0;"
         data-confirm="{% trans 'Archive this release? It stays available to any deployment that references it, but is hidden from the list.' %}">
     {% csrf_token %}
     <button type="submit" class="btn btn-sm btn-ghost" title="{% trans 'Archive release' %}">
       <svg…archive-box-icon…/>
       <span class="visually-hidden">{% trans "Archive" %}</span>
     </button>
   </form>
   ```

2. **Filter-Toggle „Show archived"** als zusätzlicher Form-Bar oberhalb der Releases-Tabelle:
   ```html
   <form class="filter-bar" method="get">
     <label class="row-gap-8" style="cursor:pointer;">
       <input type="checkbox" name="show_archived" value="1" {% if show_archived %}checked{% endif %}
              onchange="this.form.submit()">
       <span>{% trans "Show archived" %}</span>
     </label>
   </form>
   ```
   `ImageListView.get_queryset()` schaut auf `?show_archived=1` und wechselt von `ImageRelease.objects` auf `ImageRelease.all_objects` wenn gesetzt. **Per CSP**: das `onchange="this.form.submit()"` muss raus — stattdessen ein no-JS-Fallback (form mit submit-Button) ODER ein delegated change-listener in `static/js/app.js` für `[data-auto-submit]`. Wir wählen letzteres, weil's wiederverwendbar ist und dem `data-confirm`-Pattern entspricht.

3. **Archive-Spalte / Row-Highlight für archivierte Rows**: wenn `rel.archived_at` gesetzt ist, andere Row-Action (`Restore` statt `Archive`), optisch gedämpft (Klasse `is-archived`, CSS-Mute über `opacity: 0.6` oder text-color shift). Spaltenwert in „Imported" cell ergänzen um `archived {{ rel.archived_at|date:"Y-m-d" }}` wenn gesetzt.

### B.7 KPI-Tile

Die bestehende „Releases on file"-KPI auf dem Dashboard-Strip soll nur **aktive** Releases zählen (Default-Manager macht das automatisch). Eine zusätzliche neue KPI „Archived" wäre möglich aber YAGNI — wir warten bis es einen klaren Use-Case gibt. Bewusst nicht im Scope.

### B.8 Backwards-Compat / Migrations

- **Neue Migration** `apps/images/migrations/0006_imagerelease_archived_at.py`: Spalte `archived_at` (nullable, indexed) hinzufügen. Keine Constraint-Änderungen, keine Datenmigration nötig — alle existierenden Rows haben `archived_at IS NULL` per Default.
- Migration ist **forward-compatible** mit dem alten Code (vor dem Fix): der alte Code kennt `archived_at` nicht, ignoriert die Spalte. Rollback unkritisch.

### B.9 Tests

`tests/test_images_archive.py` (neu):
- `test_archive_sets_archived_at_and_clears_is_latest`
- `test_archive_is_idempotent`
- `test_restore_clears_archived_at_and_keeps_is_latest_false`
- `test_default_manager_filters_archived`
- `test_all_objects_returns_all`
- `test_archive_release_with_referenced_deployment_succeeds` (kritisch — der Kernunterschied zu Hard-Delete)
- `test_reimport_auto_restores_archived` (User's gewählte Re-Import-Policy)
- `test_archive_view_requires_admin` (security gate)
- `test_archive_view_404_on_unknown_pk`
- `test_restore_view_404_on_unknown_pk`

Bestehende `tests/test_images_delete_protected.py` Tests bleiben — hartes Löschen ist nach wie vor verfügbar und blockt korrekt bei FK-Referenzen.

---

## 3. Out of Scope

- **S3-Lifecycle für archivierte Objekte.** Objects bleiben in S3 (Hetzner Object Storage = günstig). Eine Lifecycle-Policy oder Garbage Collection für nie-mehr-genutzte archivierte Releases ist ein Folgethema sobald wirklich Volumen anfällt.
- **Bulk-Archive-Operationen.** Wenn das mal nötig wird, gibt's eine Management-Command-Routine; für jetzt: einzeln per UI.
- **OIDC_ISS_ENDPOINT ähnlich wiring.** Die `OAUTH2_PROVIDER["OIDC_ISS_ENDPOINT"]` Setting ist ein verwandter Fall (env-var-abhängig, defaults auf empty in base.py) aber dort funktioniert die Konvention: `os.environ.get("OIDC_ISS_ENDPOINT", "")` ist explizit gesetzt. Kein Fix nötig.
- **Audit anderer hardcoded URLs.** Nicht systematisch in dieser Iteration. Wenn weitere Vorkommen auffallen, eigenes Issue.

---

## 4. Tasks (high-level — feeds into Plan-Doc)

**Part A (Provisioning URL):**
1. Settings-Glue in `config/settings/base.py`
2. `run_background_jobs.py` fail-loud
3. `tests/test_provisioning_server_url.py`

**Part B (Soft-Delete):**
1. Model-Field + Migration + Manager
2. `archive()` / `restore()` Methoden
3. `ImageArchiveView` / `ImageRestoreView` + URLs
4. Image-Import-Pfad: Auto-Restore
5. UI: Button-Tausch, Filter-Toggle, archived-row-Style
6. `[data-auto-submit]` Helper in `static/js/app.js` (CSP-konform)
7. Tests `tests/test_images_archive.py`

Plan-Granularität, Reihenfolge, und Subagent-Aufteilung kommen in einem separaten `docs/superpowers/plans/2026-06-06-…-plan.md` Dokument (writing-plans Skill).
