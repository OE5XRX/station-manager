# Dev-Image über den station-manager deploybar machen — Channel-Design

**Issue:** [OE5XRX/station-manager#118](https://github.com/OE5XRX/station-manager/issues/118)
**Datum:** 2026-09-02
**Status:** Design approved, awaiting implementation plan

## Ziel

Das **Dev-Image** (mit SSH/sshfs, zum Entwickeln auf einer Station) über den
station-manager **importier- und OTA-deploybar** machen. Das produktive
(release) Image ist bewusst abgehärtet (kein SSH/sftp/sshfs) und daher zu wenig,
um darüber zu entwickeln. Primärer Nutzen: die HW-unabhängige qemu-Sim-Station
als Testbett über den Server-Weg bespielen.

**Bewusst minimaler Scope (YAGNI):** nur Import + Deploy + Sichtbarkeit. **Keine
Governance** in dieser Iteration (Grund: aktuell nur eine Test-Station). Regeln
kommen später, sobald es >1 Station gibt.

## Kern-Architekturentscheidung

Die Image-Variante (`release`, `dev`, …) ist eine **Build-Zeit-Eigenschaft** und
wird **ins Image gebacken** — nicht über ein Runtime-Config-Flag. Das ist strikt
getrennt von `control_enabled` (Runtime-Feature-Schalter).

Drei abgeleitete Prinzipien:

1. **Ein gebackener, unfälschbarer Marker** im Rootfs: `VARIANT_ID` in
   `/etc/os-release` (freedesktop-Standardfeld, im OE-`os-release`-Recipe bereits
   als `UNQUOTED_FIELD` vorgesehen; der Agent liest os-release ohnehin).
2. **Symmetrisches, selbstbeschreibendes Asset-Naming.** *Beide* Varianten tragen
   den Channel explizit im Dateinamen — auch `release`. Es gibt keinen „Sonderfall
   ohne Marker" mehr. Der Asset-Name **spiegelt** die gebackene Wahrheit, bestimmt
   sie aber nicht.
3. **Voll dynamischer Channel.** Der Channel ist ein reiner String, der end-to-end
   durchfließt: Build (`VARIANT_ID`) → Dateiname → DB (`ImageRelease.channel`) →
   Import → Heartbeat (`Station.current_image_variant`) → Frontend. Ein künftiger
   Marker (z. B. `staging`, `hardened`) läuft ohne Code-Change durch. Der Server
   **hardcodet keine Channel-Liste**, sondern liest den Channel aus dem Dateinamen.

### Asset-Naming (symmetrisch)

```
oe5xrx-<machine>-<channel>-<tag>.wic.bz2
oe5xrx-<machine>-<channel>-<tag>.wic.bz2.sha256
oe5xrx-<machine>-<channel>-<tag>.wic.bz2.bundle   (cosign)
```

Beispiele:
- `oe5xrx-qemux86-64-release-v1.4.0.wic.bz2`
- `oe5xrx-qemux86-64-dev-v1.4.0.wic.bz2`
- `oe5xrx-raspberrypi4-64-release-v1.4.0.wic.bz2`
- `oe5xrx-raspberrypi4-64-dev-v1.4.0.wic.bz2`

Dies ist ein **Breaking-Change** am prod-Asset-Namen
(`oe5xrx-<machine>-<tag>.wic.bz2` → `oe5xrx-<machine>-release-<tag>.wic.bz2`).
Akzeptiert, weil station-manager der einzige Consumer ist und Alt-Releases Legacy
sind (siehe Migration/Back-Compat).

### Machine-Namen — bewusst unangetastet

`qemux86-64` und `raspberrypi4-64` sind echte **Yocto-MACHINE-Identifier**
(BSP-Selektoren), keine freien Labels. Sie bleiben in dieser Iteration
unverändert:
- `-64` wegzulassen kollidiert mit real existierenden 32-bit-Maschinen
  (`qemux86`, `raspberrypi4`).
- Für den CM4 gibt es keine Stock-`cm4`-Maschine; er wird korrekt als
  `raspberrypi4-64` (BCM2711, identisch zum Pi4-SoC) gebaut.

Ein etwaiger freundlicher Alias-Layer oder ein echter Maschinen-Rename ist
orthogonal zu diesem Feature und wird ggf. als eigenes Issue erfasst.

## Nicht-Ziele (später, wenn >1 Station)

- Deploy-Governance „dev-Image nur auf dev-Station" (harte Regel pro
  `DeploymentResult`).
- `station.is_development`-Flag + automatisches Setzen in `seed_dev_station`.
- Mismatch-Alarm (field-Station meldet dev-Image oder umgekehrt → Alert).
- Audit/Governance des Channel-Flags.

Das Datenmodell (`ImageRelease.channel` + `Station.current_image_variant`) ist
bewusst so gewählt, dass diese Rules später **ohne Schema-Change** draufsetzen
können.

---

## Teil A — linux-image (Repo `OE5XRX/linux-image`, PR 1, zuerst)

### A1. VARIANT_ID ins Rootfs backen

**Datei:** `meta-oe5xrx-remotestation/recipes-core/images/oe5xrx-remotestation-image.bb`
(prod) und `…/oe5xrx-remotestation-dev-image.bb` (dev, `require`t prod one-way).

- Prod-Recipe: `OE5XRX_IMAGE_VARIANT ?= "release"` (weak default).
- Dev-Recipe: nach dem `require` `OE5XRX_IMAGE_VARIANT = "dev"` (überschreibt).
- Die bestehende `stamp_release()`-Postprocess-Funktion (läuft für beide, da dev
  prod require't) liest die Variable und schreibt in `/etc/os-release`:
  - `VARIANT_ID=<variant>` (unquoted, z. B. `VARIANT_ID=dev`)
  - `VARIANT="Development"` / `VARIANT="Release"` (human-readable)

**Invariante:** Der gebackene `VARIANT_ID` == der `<channel>`-Token im Dateinamen.
Diese Konsistenz ist die „gebackene Wahrheit"; der Dateiname spiegelt sie nur.

### A2. dev als signiertes Release-Asset publizieren

**Build:** `--both` (baut prod + dev in einer kas-Invocation) existiert bereits
(`remote-build.sh`, `_build.yml` `dev_image`-Input). `release.yml` reicht heute
`dev_image` **nicht** durch → muss `dev_image: true` an die build-Jobs geben.

**Artefakt-Sammlung:** `scripts/ydev/ci-collect-prod-artifacts.sh:20` filtert dev
heute raus (`-not -name "oe5xrx-remotestation-dev-image-*"`). Erweitern, sodass
dev-Artefakte zusätzlich eingesammelt und als eigenes CI-Artefakt hochgeladen
werden (Script parametrisieren oder ein `ci-collect-dev-artifacts.sh` daneben).

**Package/Sign/Upload (`release.yml`):** Pro Machine **beide** Varianten:
- Umbenennen: prod → `oe5xrx-<machine>-release-<tag>.wic.bz2`, dev →
  `oe5xrx-<machine>-dev-<tag>.wic.bz2`.
- `.sha256` erzeugen.
- cosign-Signatur per **gleicher** Chain
  (`release.yml@refs/tags/<tag>`) → `.bundle`. **Keine** Änderung an der
  Signatur-Identität; station-manager-Verifikation bleibt unverändert.
- Upload aller Assets ans GitHub-Release.

**Guards:** `l0-dev-packages-lint.sh` (one-way-Split) bleibt unangetastet —
orthogonal. Empfohlen: kleiner CI-Check/Test, dass für jede Machine *beide*
Channel-Assets (wic + sha256 + bundle) benannt und signiert erscheinen, und dass
das gebaute Rootfs den erwarteten `VARIANT_ID` trägt.

### A3. Agent-Pin (Follow-up, nicht blockierend)

Nach Merge des station-manager-PR (Agent meldet `image_variant`):
`scripts/pin-station-agent.sh` bumpen, damit die neue Agent-Version ins Image
kommt. Reihenfolge- statt Hard-Dependency (Heartbeat-Variante darf lagen).

---

## Teil B — station-manager (Repo `OE5XRX/station-manager`, PR 2)

### B1. Datenmodell

**`apps/images/models.py`:**
- `ImageRelease.channel` — `CharField(max_length=32, default="release")`, **keine
  harten `choices`** (freier Slug, damit ein neuer Channel automatisch durchläuft).
  Validierung: lowercase Slug (`[a-z0-9-]+`) auf Form-/Serializer-Ebene, nicht als
  DB-Constraint.
- Constraints aktualisieren:
  - `uniq(tag, machine)` → `uniq(tag, machine, channel)`.
  - Partial-unique „latest per machine" → „latest per `(machine, channel)`"
    (jede Variante hat ihren eigenen `is_latest` pro Machine).
- Soft-delete (`archived_at`, `archive()`/`restore()`) unverändert.

**`apps/images/models.py` — `ImageImportJob`:** `channel`-CharField (default
`release`), analog. `update_or_create`-Key wird `(tag, machine, channel)`.

**`apps/stations/models.py` — `Station`:** `current_image_variant` —
`CharField(max_length=32, blank=True, default="")`. Wird aus dem Heartbeat gesetzt.

**Migrationen** nach Projekt-Konvention (`# Generated by Django 6.0.x`):
1. `ImageRelease.channel` (+ Constraint-Umbau).
2. `ImageImportJob.channel`.
3. `Station.current_image_variant`.

**Back-Compat:** Bestehende `ImageRelease`-Rows erhalten `channel="release"`
automatisch über den Column-Default — kein Handarbeit nötig. Alt-Releases mit
Legacy-Asset-Namen (`oe5xrx-<machine>-<tag>.wic.bz2`, kein Channel-Infix) werden
vom Release-Browser **nicht** mehr als importierbar erkannt (going-forward
symmetric only). Bereits importierte Rows bleiben gültig. Ein reiner
Legacy-Fallback-Match wird **bewusst nicht** eingebaut (YAGNI).

### B2. Channel-Discovery statt Hardcode

**`apps/images/github_releases.py` — `GitHubRelease`:**
`has_assets_for(machine)` → Channel-Discovery. Für eine gegebene Release (Tag)
und Machine:
- Enumeriere Assets, die auf `oe5xrx-<machine>-*-<tag>.wic.bz2` matchen und die
  `.sha256`- und `.bundle`-Geschwister besitzen.
- **Channel-Extraktion (robust, nicht am `-` splitten!):** bekannten Prefix
  `oe5xrx-<machine>-` und bekannten Suffix `-<tag>.wic.bz2` abziehen → Rest =
  `channel`. (Nötig, weil `machine` selbst Bindestriche enthält.)
- Neue Methode liefert die Menge der verfügbaren `(channel)` je `(machine, tag)`.

Der Release-Browser listet daraufhin pro Release die vorhandenen
`(machine, channel)`-Kombinationen als importierbare Einträge.

### B3. Storage + Import-Worker

**`apps/images/storage.py`:** `release_key`, `release_bundle_key`,
`release_rootfs_key` um `channel` erweitern → varianten-getrennter S3-Pfad, z. B.
`images/<tag>/<channel>/<machine>.wic.bz2`.

**`apps/provisioning/management/commands/run_background_jobs.py`:**
- `fetch_release_asset` baut den channel-korrekten Asset-Namen.
- `update_or_create` keyed auf `(tag, machine, channel)`, setzt `channel`.
- cosign-Verifikation (`apps/images/cosign.py`) **unverändert** (variant-agnostisch,
  gleiche Chain).
- rootfs-Extraktion (`apps/images/extraction.py`, GPT/`root_a`) **unverändert**
  (variant-agnostisch).

### B4. Frontend-Sichtbarkeit (Badge überall wo sinnvoll)

Channel-/Variant-Badge als Bootstrap-Pill (bestehende `.pill pill-*`-Konvention,
`static/css/app.css` ~L509–560). `release` dezent (z. B. `pill-muted`), abweichende
Channels prominent (z. B. `pill-accent`) — Ziel: `dev` fällt sofort auf.

**Vollständige Anzeigestellen** (aus Codebase-Sweep):

*(a) ImageRelease / Import-Browser:*
- `apps/images/templates/images/image_list.html` (Row, neben Tag/Machine-Pill).
- `apps/images/templates/images/_github_release_row.html` (Import-Browser-Row).
- `apps/provisioning/templates/provisioning/_provisioning_section.html`
  (Image-Select-Dropdown-Label, z. B. `<tag> [<channel>]`).

*(b) Station-laufendes Image (aus Heartbeat `current_image_variant`):*
- `apps/stations/templates/stations/station_detail.html` (Summary-Bar: neben
  „Reported OS version" und neben „Provisioned with"-Tag).
- `apps/stations/templates/stations/_station_table.html` (OS-Spalte).
- `apps/dashboard/templates/dashboard/index.html` (Live-Fleet-Tabelle, OS-Spalte).

*(c) Deployment / Rollout:*
- `apps/deployments/templates/deployments/deployment_list.html` (Ziel-Image).
- `apps/deployments/templates/deployments/deployment_detail.html` (Header + Config-Sidebar).
- `apps/rollouts/templates/rollouts/upgrade_dashboard.html` (latest-per-machine,
  up-to-date-Tabelle).
- `apps/rollouts/templates/rollouts/_dashboard_row.html` (current + target Tag).
- `apps/rollouts/templates/rollouts/_station_upgrade_card.html` (current + target +
  recent deployments).

*(d) Echtzeit/JSON:*
- `apps/deployments/consumers.py` (WebSocket-Payload ~L72–73): `channel` optional
  in die JSON aufnehmen, damit JS-gerenderte Komponenten es zeigen können.

**View-Hinweis:** Sicherstellen, dass `channel` in den relevanten
`select_related`/Context-Dicts verfügbar ist (Station-Detail/-Liste,
Deployment-/Rollout-Views, Provisioning-Form).

### B5. Heartbeat (jetzt mit rein)

**Agent (`station_agent/`, im selben Repo):** liest `VARIANT_ID` aus
`/etc/os-release` und meldet `image_variant` im Heartbeat-Payload. Fallback bei
fehlendem Feld: leerer String.

**`apps/api/serializers.py` — `HeartbeatSerializer`:** optionales Feld
`image_variant = CharField(max_length=32, required=False, default="")`.

**`apps/api/views.py` (Heartbeat-View):** `station.current_image_variant =
data.get("image_variant", "")` neben den bestehenden Feldern setzen.

Basis für spätere Mismatch-Erkennung (Non-Goal dieser Iteration).

### B6. OTA-Deploy verifizieren

Der Deploy-Pfad ist varianten-agnostisch (GPT/`root_a`-Extraktion, gleiche
Struktur). **Kein Code-Change erwartet**, aber im Test-Scope: dev-Release
erkennen → import → rootfs-extract → (sim) deploy → Station bootet dev und meldet
`image_variant=dev`.

---

## Datenfluss (end-to-end)

```
kas build (dev)  ──VARIANT_ID=dev──►  /etc/os-release  (gebackene Wahrheit)
      │
release.yml  ──►  oe5xrx-<machine>-dev-<tag>.wic.bz2 (+.sha256 +.bundle, cosign)
      │
station-manager Release-Browser  ──Channel-Discovery──►  (machine, channel=dev)
      │
Import-Worker  ──cosign verify (unverändert)──►  ImageRelease(channel="dev")
      │                                              rootfs-extract (unverändert)
OTA-Deploy (varianten-agnostisch)  ──►  Station bootet dev-Rootfs
      │
station-agent  ──liest VARIANT_ID──►  Heartbeat image_variant="dev"
      │
Server  ──►  Station.current_image_variant="dev"  ──►  Badge im Frontend (überall)
```

## Fehlerbehandlung / Edge-Cases

- **Marker vs. Dateiname divergiert:** Der Server vertraut dem Dateinamen für den
  DB-`channel` (Import), aber dem gebackenen `VARIANT_ID` für die *laufende*
  Variante (Heartbeat). Divergenz wird in dieser Iteration nur *sichtbar* (beide
  Werte im Frontend), nicht alarmiert — Fundament für spätere Mismatch-Detection.
- **Release ohne Channel-Assets (Legacy):** Browser zeigt sie nicht als
  importierbar; kein Crash, kein Legacy-Fallback (bewusst).
- **Fehlendes `image_variant` im Heartbeat (alter Agent):** `current_image_variant`
  bleibt leer; Frontend zeigt kein Badge für „laufend". Kein Fehler.
- **Unbekannter Channel-String:** wird als freier Slug akzeptiert und angezeigt
  (dynamisches Design) — keine Enforcement.

## Test-Strategie

**linux-image:**
- CI-Check: pro Machine erscheinen beide Channel-Assets (wic+sha256+bundle),
  korrekt benannt und signiert.
- Rootfs trägt erwarteten `VARIANT_ID` (release vs. dev).

**station-manager (Unit):**
- `has_assets_for`/Channel-Discovery: korrekte Extraktion trotz Bindestrichen in
  `machine`; erkennt dev + release; ignoriert unvollständige Asset-Sets.
- Model-Constraints: `uniq(tag, machine, channel)`; latest-per-`(machine, channel)`.
- Import-Worker: channel-keyed `update_or_create`, `channel` gesetzt, S3-Keys
  channel-getrennt.
- `HeartbeatSerializer`: optionales `image_variant`; View setzt
  `current_image_variant`.

**station-manager (E2E, probe):**
- dev-Release erkennen → import → rootfs-extract → (sim) deploy → Heartbeat
  `image_variant=dev` → Badge sichtbar. Voller Server-Weg über die qemu-Sim-Station.

## Sequencing

1. **linux-image-PR** (A1–A2): dev-Asset symmetrisch benannt, signiert,
   publiziert; `VARIANT_ID` gebacken.
2. **station-manager-PR** (B1–B6): Model, Discovery, Import, UI, Heartbeat.
3. **Follow-up:** `pin-station-agent.sh`-Bump in linux-image (A3).

Je ein PR pro Repo (Cross-Repo-Feature). Squash-Merge-Konvention pro Repo.
