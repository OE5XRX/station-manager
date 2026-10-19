# Rollout-Gruppen mit Update-Kanal + Plattform von der Station + Fix 500er Rollout-Sequenz

Datum: 2026-10-19 · Branch `feature/rollout-groups` · ein PR (Spec + Plan + Code)

## Warum

Zwei Dev-Stationen (qemux86-64 + raspberrypi4-64) sollen die jeweils neuesten
**Dev-Images** bekommen. Heute zielt jeder Upgrade-Pfad hart auf
`ImageRelease(machine=…, channel="release", is_latest=True)`
(`UpgradeStationView`, `UpgradeGroupView`, Upgrade-Dashboard, Upgrade-Karte auf
der Station-Detailseite). Tags sind gleichzeitig freie Labels **und** funktionale
Rollout-Gruppierung (Sequenz, Deploy „By Tag", Modul-FW-Scope/Canary); eine
Station kann n Tags tragen, die Zuordnung „welche Gruppe gilt" ist daher implizit
(first-match-wins in der Sequenz). Ein Kanal pro Station lässt sich darauf nicht
sauber setzen.

Plus: HTTP 500 beim Hinzufügen eines Tags zur Rollout-Sequenz.

## Bug 500 (bereits gefixt, Commit `b705f05`)

**Ursache:** `SequenceAddView` berechnete die nächste Position als
`(Max("position") or -1) + 1`. Bei **genau einem** Eintrag ist `Max == 0`;
`0 or -1` ergibt `-1`, die neue Position wird wieder `0` →
`IntegrityError` auf `uniq_position_per_sequence` → 500. Betroffen war also immer
das Hinzufügen des **zweiten** Eintrags (leere Sequenz und ≥2 Einträge gehen).
Der Fehler ist seit #21 latent, **keine** Regression aus #164. Der API-Pfad
(`apps.rollouts.services.add_entry`, #155) hatte den Fix bereits (expliziter
`None`-Check); die UI-View behielt ihre eigene Kopie („shared extraction is a
tracked follow-up"). Fix: UI Add/Remove delegieren an `apps.rollouts.services`.
Repro/Regression: `tests/test_rollout_sequence_add_500.py` (0/1/2 Vor-Einträge;
staff/member → 403, nicht 500 — `rollouts.manage` ist Admin).

## Ziele

1. Neues Konzept **Rollout-Gruppe** (`RolloutGroup`). `StationTag` bleibt freies
   Label ohne Funktion.
2. Station in **0..1** Gruppe (`Station.rollout_group`, FK, `PROTECT`). Ohne
   Gruppe = Kanal `release`, keine Sequenzposition.
3. Gruppe hat **Kanal** (`release` | `dev`, dieselben Werte wie
   `ImageRelease.channel`) und optional eine **Position in der Rollout-Sequenz**
   (die Sequenz ordnet Gruppen statt Tags).
4. Alle funktionalen Tag-Nutzungen → Gruppe:
   - Rollout-Sequenz (`RolloutSequenceEntry.tag` → `.group`), Forms/Views/Grouping/Signals.
   - Gruppen-Upgrade + Upgrade-Dashboard.
   - Deployment „By Tag" → „By Group" (`target_tag` → `target_group`,
     `TargetType.TAG` → `TargetType.GROUP = "group"`).
   - Modul-FW-Targets: `scope=tag` → `scope=group`, `tag` → `group`,
     `canary_tag` → `canary_group`; Präzedenz station > group > fleet;
     Constraints (uniq per type+group, Scope/Ref-Kohärenz, canary nur fleet) angepasst.
5. **Upgrade-Ziel** = `is_latest`-ImageRelease im Kanal der Gruppe für die
   Plattform der Station, muss OTA-fähig **und strikt neuer** als das aktuelle
   Image sein. Kein Downgrade: nicht neuer → kein Angebot, klare Meldung.
   Upgrade bleibt manuell.
6. **Plattform von der Station**, keine UI-Abfrage beim Upgrade.
7. Datenmigration, die nie abbricht (siehe unten).
8. API ohne Kompat-Layer + OpenAPI.

## Nicht-Ziele / Anti-Ziele

- Kein Auto-Rollout, kein Wartungsfenster.
- Kein Umbau Image-Import / Provisioning (Provisioning behält Plattform-Auswahl).
- Keine Änderung an `linux-image` / `station_agent`.
- Kein Umbau der Modul-FW-Zustandsanzeige; in `apps/module_firmware` nur
  Target-Scope/Canary.
- Anti-Ziel: ein Upgrade, das eine Station auf ein **älteres** Image oder (bei
  dev-Gruppe) auf ein Release-Image setzt. Anti-Ziel: Migration, die bei
  unerwarteten Altdaten crasht.

## Entscheidungen (meine Calls, wo der Brief offen ließ)

### Modellort: `apps/stations` (neben `StationTag`)
`RolloutGroup` ist eine Station-Eigenschaft (wie Region/Tag), die Stations-UI
(Liste/Detail/Form) und die Tag-CRUD liegen dort; die Rollout-App referenziert sie
nur (`rollouts → stations` existiert schon, kein neuer Migrationszyklus).

```python
class RolloutGroup(models.Model):
    class Channel(models.TextChoices):
        RELEASE = "release", _("Release")
        DEV = "dev", _("Dev")
    name = CharField(max_length=100, unique=True)
    slug = SlugField(unique=True)          # RESERVED_TAG_SLUGS gilt auch hier (CheckConstraint)
    channel = CharField(max_length=32, choices=Channel.choices, default="release")
    description = TextField(blank=True)
    created_at = DateTimeField(auto_now_add=True)
    ordering = ["name"]
Station.rollout_group = FK(RolloutGroup, null=True, blank=True, PROTECT, related_name="stations")
```
Kanal als `TextChoices` (dev/release) — `ImageRelease.channel` ist free-form, aber
für Gruppen wollen wir nur die zwei realen Kanäle; Vergleich ist ein String-Equal
auf `ImageRelease.channel` (Wiederverwendung der Kanal-Logik aus #120).

Permission: Gruppen-CRUD = `stations.manage` (Staff, wie StationTag). Station →
Gruppe zuordnen = Station bearbeiten (`stations.manage`). Sequenz bearbeiten +
Gruppen-Upgrade bleiben `rollouts.manage` (Admin). Einzel-Upgrade bleibt
`deployments.manage`.

### Plattform-Quelle: `Station.current_image_release.machine`
Der Agent-Heartbeat meldet **kein** MACHINE (Payload: `os_version` = PRETTY_NAME,
`image_variant` = VARIANT_ID, kein Machine-Feld; `linux-image` stempelt MACHINE
nicht in `/etc/os-release`). Die Station trägt aber ihr laufendes Image:
`current_image_release` wird beim Provisioning-Download gesetzt und nach **jedem
verifizierten OTA** auf das deployte Release umgestellt
(`apps/deployments/api_views.py`). `machine` dieses Releases ist die Plattform.
Das ist die bestehende Quelle der heutigen Release-Pfade — keine UI-Abfrage.
Station ohne `current_image_release` → klare Meldung „Plattform unbekannt —
Station wurde nicht über station-manager provisioniert" (kein Raten).
Folge-Option (nicht hier, braucht User-OK, Agent-Änderung): MACHINE in os-release
stempeln + im Heartbeat melden.

### „Neuer als": Tag-Schlüssel mit Fallback
Image-Tags sind `YYYY.MM.DD-HH` (dev und release teilen den Tag, ein GH-Release
trägt beide Assets). `release_key(tag)` = Tupel aller Ziffernfolgen
(`2026.10.19-04` → `(2026, 10, 19, 4)`). Kandidat ist neuer, wenn
`release_key(candidate) > release_key(current)`; haben beide keine Ziffern →
`imported_at`-Vergleich. Gleicher Tag in anderem Kanal ist **nicht** neuer
(release → dev wechselt erst beim nächsten Build; strikt nach „nicht neuer →
nichts anbieten").

### Zentrale Ziel-Auflösung: `apps/rollouts/targeting.py`
```python
station_channel(station) -> str                 # group.channel oder "release"
station_platform(station) -> str | None         # current_image_release.machine
latest_index() -> dict[(machine, channel), ImageRelease]   # ein Query, is_latest=True
resolve_upgrade(station, index=None) -> UpgradeTarget(target, reason)
  reason ∈ {"ok", "no_platform", "no_release", "not_ota_ready", "up_to_date", "not_newer"}
is_newer(candidate, current) -> bool
```
`target` ist bei `ok` das Ziel; bei `up_to_date`/`not_newer`/`not_ota_ready`
trotzdem gesetzt (für die Anzeige). Benutzt von: `UpgradeStationView`,
`UpgradeGroupView`, `UpgradeDashboardView`, Station-Detail-Upgrade-Karte.
`up_to_date` = `current_image_release_id == target.pk`.

### Dashboard / Gruppen-Upgrade
Bucketing trivial über `station.rollout_group`: Sequenz-Gruppen in Positions-
reihenfolge, dann Gruppen ohne Sequenzposition (alphabetisch), dann „Ohne Gruppe"
(Sentinel-Key `__unassigned__`, Kanal release). Jede Gruppen-Überschrift zeigt
Kanal-Badge. Gruppen-Upgrade (`upgrade/group/<slug>/`) funktioniert für jede
Gruppe (Sequenz-Mitgliedschaft nicht mehr nötig — first-match-wins entfällt, weil
0..1); erzeugt pro Plattform einen Deployment `target_type=group`; übersprungen
werden Stationen ohne Plattform, ohne neueres/OTA-fähiges Ziel, bereits aktuelle
und Konflikte. Flash-Message zählt queued/skipped.

### Deployment „By Group"
`TargetType`: `GROUP = "group", "By Group"` ersetzt `TAG`. `target_group` FK
(`SET_NULL`). `get_target_stations` → `Station.objects.filter(rollout_group=…)`.
Historie: Migration mappt jedes `target_tag` auf die gleichnamige Gruppe, setzt
`target_type="group"`; Deployments mit gelöschtem Tag (`target_type="tag"`,
`target_tag=NULL`) → `group` mit `NULL`. Anzeige „By Group: <name>" bzw. „By Group
(gelöscht)". Deploy-Form-Validierung parallel zu heute (group required bei group).

### Modul-FW
`ModuleFirmwareTarget.Scope.GROUP = "group"`; Felder `group` (FK `RolloutGroup`,
`PROTECT`) und `canary_group` (`PROTECT`). Constraints: `uniq_group_target_per_type_group`,
`target_group_has_group_only`, `target_station_has_station_only` (station darf
keine group), `canary_group_only_on_fleet`. Reconciler: station-Target, sonst
group-Target für `station.rollout_group_id`, sonst fleet; Canary-Gate:
`fleet.canary_group_id is not None and station.rollout_group_id != canary_group_id`
→ kein Target. Ops-/Audit-Strings („tag <slug>" → „group <slug>").

### Datenmigration (bricht nie ab)
In `apps/rollouts` (abhängig von den Schema-Migrationen in stations, deployments,
module_firmware; die Drop-Migrationen hängen danach):
1. Funktionale Tags = Tags in Sequenz-Einträgen ∪ `Deployment.target_tag` ∪
   `ModuleFirmwareTarget.tag` ∪ `.canary_tag`.
2. Pro funktionalem Tag eine Gruppe: `name`/`slug` = Tag, `channel="release"`,
   `description` = Tag-Beschreibung. Kollision (Gruppe mit Name/Slug existiert
   schon, z. B. Re-Run) → bestehende wiederverwenden (`get_or_create` per slug,
   Name-Kollision → Suffix). Idempotent.
3. Sequenz-Einträge: `group` = Gruppe des Tags, Position bleibt.
4. Stationen: alle funktionalen Tags der Station sammeln. 0 → keine Gruppe;
   1 → diese; n → Gruppe des Tags mit kleinster Sequenzposition, Tags ohne
   Sequenzposition danach, Tie-Break Tag-Name (deterministisch). Bei n>1:
   `StationAuditLog` (`event_type="rollout_group_changed"`) mit Liste der Tags und
   der gewählten Gruppe. Station mit bereits gesetzter Gruppe wird nicht
   überschrieben.
5. Deployments + Modul-FW-Targets: FK-Mapping wie oben.
6. Tags + Station-Tag-Zuordnungen bleiben unverändert (Labels).
Jeder Schritt robust gegen Nullwerte/verwaiste Referenzen; reverse = noop
(Schema-Reverse rekonstruiert keine Tags).

Neuer `StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED` — auch geloggt, wenn die
Gruppe einer Station per UI (Station-Form) oder API geändert wird.

### API (kein Kompat-Layer)
- Neue Ressource `rollout-groups` (read: `stations.view`; write CRUD:
  `stations.manage` wie StationTag), Felder `id, name, slug, channel,
  description, created_at`; Audit wie StationTag (`audit_config_write`).
- Station: neues Feld `rollout_group` (PK, schreibbar wo Station schreibbar),
  Filter `rollout_group`. `tags` bleibt.
- RolloutSequenceEntry: `tag` → `group` (Filter ebenso).
- Deployment read: `target_tag` → `target_group`; Create bleibt station-only.
- Module-FW-Targets: `tag` → `group`, `canary_tag` → `canary_group`,
  `scope` `group`.
- OpenAPI via drf-spectacular aktualisiert; Schema-Tests angepasst.
- Permission-Matrix-Tests für `rollout-groups` (Rollen admin/staff/member/
  applicant/anon × list/create/update/delete) im Muster #155/#164.

### UI (Skill frontend-design Pflicht)
- Gruppen-CRUD (Liste/Form/Löschen) unter Stations, Sidebar-Eintrag neben Tags;
  Liste zeigt Kanal-Badge, Anzahl Stationen, Sequenzposition.
- Station-Form: Select „Rollout-Gruppe" (leer = keine Gruppe, release).
- Station-Liste/-Detail: Gruppe + Kanal-Badge (bestehendes
  `images/_channel_badge.html`).
- Upgrade-Karte (Station-Detail): Ziel + Kanal, bzw. Grund-Meldung
  (Plattform unbekannt / kein Image im Kanal / nicht neuer / nicht OTA-fähig /
  aktuell).
- Upgrade-Dashboard: Gruppen mit Kanal-Badge; Ziel pro Station aus dem Kanal.
- Sequenz-Editor: Gruppen statt Tags.
- Deployment-Form/-Liste/-Detail: „By Group".
- Modul-FW-Target-Form/-Liste: Gruppe/Canary-Gruppe.

## Akzeptanz

- Station in dev-Gruppe: Ziel = neuestes dev-Image passender Plattform, nie
  release, nie älter.
- Ohne Gruppe / release-Gruppe: neuestes release, kein Downgrade.
- Keine Plattform-Abfrage beim Upgrade; Provisioning unverändert.
- Sequenz ordnet Gruppen; add/remove/reorder ohne 500; Repro-Test + Ursache im PR.
- Deploy by group + Modul-FW group/Canary-Gruppe; Präzedenz getestet.
- Migration getestet: 0/1/n Tags, Audit bei n, bricht nie ab (verwaiste/gelöschte
  Referenzen), Tags bleiben.
- API + OpenAPI; Permission-Matrix-Tests `rollout-groups`.
- `python -m pytest -q` + CI grün, review-loop bis 0.

## Nachtrag (Review-Runde 1): Löschen + Kanal

- **Gruppe löschen ist blockiert**, solange Stationen, Sequenz-Einträge,
  Modul-FW-Gruppen-Targets oder Canary-Gates darauf zeigen: alle vier FKs sind
  `PROTECT`. UI-Bestätigungsseite listet die Referenzen statt eines Buttons; API
  → 409; Django-Admin zeigt seine Protected-Seite. Nur `Deployment.target_group`
  bleibt `SET_NULL` (Name-Snapshot hält die Historie lesbar). Damit kann ein
  gelöschtes Canary-Gate kein Fleet-Target mehr still fleet-weit ausrollen.
- **Kanal ist nach dem Anlegen fix**: UI-Feld disabled, API → 400 bei Änderung.
  Kanalwechsel nur explizit (andere Gruppe oder bewusstes Deployment).
- Gruppen anlegen/ändern/löschen schreibt in UI und API eine `CONFIG_CHANGED`-Zeile
  (UI mit Feld-Diff).
