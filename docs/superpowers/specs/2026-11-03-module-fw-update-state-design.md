# Modul-FW: Update-Zustand ehrlich anzeigen + Lifecycle im Audit-Trail

**Status:** Spec, abgestimmt mit dem User (2026-10-19 Briefing, 2026-11-03 Spec-Runde).
**Repos:** station-manager (Hauptteil, inkl. `station_agent`), FW-RemoteStation (describe-Feld).

## 1. Warum

Am 2026-10-19 stand ein echtes FM-Modul in der Modul-Liste auf „updating“. Im Modul-Audit-Trail stand nichts, es gab weder Fortschritt noch Fehler. Ursache: Die Station lief auf einem alten Image, dessen Agent gar keine Module flashen kann. Heute bedeutet „updating“ nur „Ist-Version ≠ Soll-Version“, nicht „Flash läuft“.

Zweitens: Module mit Debug-FW haben einen MCUboot mit **Dev-Key**. Release-FW ist prod-signiert. Der DFU-Download läuft durch, aber MCUboot verweigert den Swap. Das endet nach 3× rolled_back in der Quarantäne: drei nutzlose Flash-Zyklen mit Station-Drain, und als Grund steht nur „rolled_back“. MCUboot ist im Feld nicht updatebar, die Lösung ist ein SWD-Reflash an der Bench.

**Erfolg:** Wer ein Modul ansieht, erkennt sofort, ob etwas passiert, warum nichts passiert, oder was schiefging. Der Audit-Trail erzählt jeden Flash-Versuch vollständig. Unmögliche Updates werden gar nicht erst versucht.

## 2. Ist-Stand (origin/main, verifiziert 2026-11-03)

- `Module.firmware_convergence` (`apps/module_firmware/models.py`) hat die Werte ok, updating, quarantined und unknown. Die Rollup-Logik steckt in `reconciler.reconcile_module()`. Ohne passende Varianten-Release wird trotzdem `updating` gesetzt, und es entsteht keine Zeile.
- `ModuleFirmwareConvergenceState` (je Modul × Ziel-Release) hat `state` (ok, updating, quarantined), `attempts`, `transient_failures`, `last_error_mode`, `last_error_message` und `last_attempt_at`.
- `record_error`: transient zählt nur `transient_failures++` und quarantäniert nie; rolled_back führt ab 3 Versuchen in die Quarantäne, rejected sofort.
- `drain.py`: `pending_updates()` ignoriert Zeilen mit `transient_failures >= DRAIN_TRANSIENT_LIMIT (5)`. Der Drain endet dann still, die Zeile bleibt aber `updating`.
- `reconcile_api_views.py`:
  - **check** liefert 204, wenn ein `ControlLock` gehalten wird oder kein `executable_artifact()` existiert.
  - check gibt Zeilen über dem Transient-Limit **weiterhin aus**: Die Station versucht es selbst opportunistisch, nur der Drain ist dann aus (Kommentar `models.py:7-12`).
  - **status** akzeptiert downloading, flashing, verifying, failed, rolled_back und rejected.
  - **commit** schließt den Versuch ab.
- **Audit:** `StationAuditLog` hat einen Modul-FK. Es gibt die Events MODULE_FLASH_STARTED, SUCCESS, ROLLED_BACK, REJECTED und FAILED sowie MODULE_QUARANTINED und MODULE_QUARANTINE_RELEASED. Angezeigt wird das in `module_detail.html` (Audit-Sektion), im Station-Audit-Tab und in `apps/audit`.
- **Capability:** `Station.module_ota_backends()` aus dem Heartbeat (`capabilities.module_ota.backends`). Ein alter Agent meldet nichts.
- **Online:** `Station.is_online` (last_seen < 120 s). **Belegt:** `ControlLock` mit Holder.
- **Anzeige der Konvergenz-Pille** (`_convergence_pill.html`): nur Modul-Detail und Konvergenz-Seite. Die Modul-Liste und die Station-Modulkarte (`control/_module_card.html`) zeigen sie nicht.
- **Agent:** `station_agent/slot_discovery.py` reicht aus `MODULE-DESCRIBE` nur `id`, `identity` und `capabilities` durch.
- **FW:** `module <id> describe` gibt `MODULE-DESCRIBE {"schema":2,"module":..,"identity":{type,model,version,variant,uid,uid_source},"capabilities":[..]}` aus (`include/oe5xrx/module/iface.h`, `sa818_module.cpp`). Zur Laufzeit gibt es keinerlei Key-Information. Signiert wird mit ECDSA-P256; Release-Builds nutzen den Prod-Key aus einem Secret, lokale und CI-Builds den MCUboot-Dev-Key `root-ec-p256.pem`.

## 3. Ziele

1. **Getrennte, verständliche Zustände** in Modul-Liste, Modul-Detail, Station-Modulkarte und API (§4).
2. **Vollständiger Audit-Trail** jedes Update-Lebenszyklus, ohne Heartbeat-Spam (§6).
3. **Fehlerzustand nach N transienten Fehlern** (N = `DRAIN_TRANSIENT_LIMIT` = 5) statt ewig „updating“, dazu ein Button „Erneut versuchen“ (§5.3).
4. **Bootloader-Key-Mismatch vor dem Flash erkennen:** keine Anweisung, kein Drain, Zustand „Nicht updatebar“ (§7).

## 4. Zustandsmodell (User-Entscheidung)

Anzeige-Zustand je Modul, Priorität von oben nach unten (der erste Treffer gewinnt):

| # | Anzeige | Bedingung | Herkunft |
|---|---|---|---|
| 1 | **Kein Ziel** | kein effektives Target | wie heute (`unknown`) |
| 2 | **Aktuell** | reported_version = Soll | wie heute (`ok`) |
| 3 | **Quarantäne (\<Grund\>)** | Zeile `quarantined` | gespeichert |
| 4 | **Nicht updatebar: \<Grund\>** | Zeile `not_updatable` | gespeichert (neu) |
| 5 | **Fehler: \<letzte Meldung\>** | Zeile `failed` | gespeichert (neu) |
| 6 | **Wartet: Station offline** | Station nicht online | abgeleitet |
| 7 | **Update läuft: \<Phase\>** | Zeile hat `phase` ∈ {downloading, flashing, verifying} | gespeichert (neu) |
| 8 | **Wartet: \<Grund\>** | siehe unten | abgeleitet |
| 9 | **Update ausstehend** | Drift, nichts blockiert | abgeleitet |

**Wartegründe**, jeweils mit `reason_code` und deutschem Text:
- `agent_no_module_ota`: „Station-Agent kann keine Module flashen (Image zu alt)“. Gilt, wenn das benötigte Backend (dfu bzw. sim) nicht in `module_ota_backends()` steht.
- `no_release_for_variant`: „Keine Firmware \<Version\> für Variante \<Variante\> vorhanden“. Gilt, wenn es keine Release für die Modul-Variante gibt.
- `no_artifact`: „Release enthält kein passendes Artefakt (\<dfu|sim\>)“. Gilt, wenn die Release existiert, aber `executable_artifact()` None liefert, obwohl das Backend da ist.
- `station_offline`: „Station offline“.
- `station_busy`: „Station belegt (Bedienung aktiv)“. Gilt, wenn ein `ControlLock` mit Holder existiert. Das ist ohne Zusatzlast ableitbar, weil check denselben Test schon macht.

**Hängende Phase:** Geht die Station während „Update läuft“ offline, zeigt die UI wegen der Priorität 6 vor 7 „Wartet: Station offline“ statt einer eingefrorenen Phase. `phase` bleibt gespeichert. Ein neuer Versuch (eine neue Anweisung) überschreibt sie, ein Commit oder Fehler löscht sie.

**Umsetzung:** Eine zentrale, reine Funktion `module_update_status(module, *, station=None) -> UpdateStatus(state, phase, reason_code, reason_text, since)` in `apps/module_firmware/`. UI und API rufen nur diese Funktion auf. Für Listen muss sie mit vorab geladenen Daten (prefetch/select_related) ohne N+1 auskommen; wie, entscheidet der Builder.

## 5. Datenmodell und Server-Logik

### 5.1 Modell-Änderungen (meine Entscheidung)
- `ModuleFirmwareConvergenceState.state` bekommt zusätzlich **`failed`** und **`not_updatable`**.
- Neue Felder an `ModuleFirmwareConvergenceState`:
  - `phase` (CharField, blank; downloading, flashing, verifying)
  - `phase_at` (DateTime, null)
  - `reason_code` (CharField, blank; für not_updatable bzw. failed)
- `Module.firmware_convergence` bekommt dieselben zwei neuen Werte, damit Filter, Dashboard und API konsistent bleiben.
- `ModuleFirmwareRelease` bekommt **`boot_key_hash`** (CharField, blank; Hex-Präfix, §7). Für das Sim-Artefakt gibt es kein solches Feld: Es ist ein cosign-signiertes native_sim-ELF ohne MCUboot-TLV. Synthetische Module (Artefakt sim) überspringen den Key-Check deshalb immer.
- `Module` bekommt **`reported_boot_key`** (CharField, blank), befüllt aus dem Inventar.
- Migrationen müssen additiv sein. Bestehende `updating`-Zeilen mit `transient_failures >= 5` werden per Datenmigration auf `failed` gesetzt (`reason_code=transient_limit`).

### 5.2 Reconciler
- Gibt es keine Release für die Variante, setzt der Reconciler den Rollup weiterhin auf `updating` (Drift). Die Anzeige leitet daraus „Wartet: keine Firmware …“ ab. Die Target-Auflösung aus #169 wird nicht angefasst.
- Key-Check (§7), nur für das Artefakt dfu:
  - Sind beide Hashes bekannt und verschieden: Zeile anlegen bzw. auf `not_updatable` setzen (`reason_code=boot_key_mismatch`), Audit-Eintrag nur beim Übergang.
  - Der Mismatch **überschreibt auch `failed` und `quarantined`**, weil er die präzisere Diagnose ist und kein Versuch je gelingen kann *(meine Entscheidung)*.
  - Werden die Hashes später gleich, z. B. nach einem Bench-Reflash, wird **nur `not_updatable`** zurück auf `updating` gesetzt (Audit MODULE_UPDATABLE_AGAIN), mit `attempts` und `transient_failures` auf 0. Quarantäne und `failed` werden dadurch nie automatisch freigegeben.
- `failed` und `not_updatable` sind „sticky“ wie die Quarantäne: Ein Heartbeat setzt sie nicht zurück. Erreicht die reported_version den Sollwert, gilt dagegen immer `ok`.
- **Neues Ziel:** Es entsteht eine neue Zeile, also startet der Zustand frisch. Das ist schon heute so, weil die Zeilen je Ziel-Release angelegt werden.

### 5.3 Fehlerzustand und „Erneut versuchen“
- „N transiente Fehler“ heißt **N in Folge**: Die bestehenden Resets von `transient_failures` (bei `verifying` und bei erfolgreichem Commit) bleiben erhalten.
- In `record_error(mode=transient)` gilt: Erreicht `transient_failures` den Wert `DRAIN_TRANSIENT_LIMIT`, wird `state=failed` und `reason_code=transient_limit` gesetzt, die letzte Meldung bleibt in `last_error_message`. Dazu kommt ein Audit-Eintrag `MODULE_FLASH_GAVE_UP` (Name entscheidet der Builder). Der Drain ist damit frei, weil `pending_updates` nur `updating` zählt.
- **`failed` stoppt wirklich (User-Entscheidung 2026-11-03, Variante a):** check gibt für `failed` keine Anweisung mehr aus. Das stille opportunistische Weiterprobieren von heute entfällt bewusst. Weiter geht es nur per „Erneut versuchen“ oder mit einem neuen Ziel (neue Zeile). Dasselbe gilt für die Datenmigration aus §5.1: Bestehende Zeilen über dem Limit werden `failed` und hören damit auf, im Feld weiterzuprobieren.
- **Retry (User-Entscheidung):** POST-Action analog zu `services.release_quarantine`, mit denselben Rechten und demselben UI-Ort (Modul-Detail). Sie setzt die Zeile zurück auf `updating`, `transient_failures=0` und `phase=''` und schreibt den Audit-Eintrag `MODULE_FLASH_RETRY` mit dem User. Sie gilt nur für `failed`. Quarantäne behält ihre eigene Freigabe, `not_updatable` hat bewusst keinen Retry. Die API bekommt die gleiche Action, analog zur bestehenden `release_quarantine`-Action in `apps/api/module_firmware_views.py`.

### 5.4 Check, Status und Commit
- **check:** Liefert weiterhin 204 bei Lock, bei fehlendem Artefakt und bei `not_updatable`, `failed` oder `quarantined`. Wenn eine Anweisung ausgegeben wird: Audit `MODULE_FLASH_INSTRUCTED`, aber nur, wenn für diese Zeile seit dem letzten Status-POST bzw. Commit noch keine Anweisung geloggt wurde. Damit gibt es keinen Spam bei wiederholtem Polling derselben offenen Anweisung; den Mechanismus legt der Builder fest, z. B. ein Feld `instructed_at`.
- **status:** Eine Zeile in `failed` oder `not_updatable` bekommt wie bei der Quarantäne 409 und keine Zustandsänderung; das 409 wird als MODULE_FLASH_CONFLICT geloggt *(meine Entscheidung)*.
  - downloading, flashing, verifying setzen `phase` und `phase_at`. Audit gibt es pro Phasenwechsel, nicht bei einer Wiederholung derselben Phase. MODULE_FLASH_STARTED bleibt für den ersten Status.
  - failed, rolled_back und rejected löschen `phase` und loggen wie heute, mit Fehlertext.
- **commit:** löscht `phase`, loggt wie heute SUCCESS bzw. ROLLED_BACK. Ein 409 auf quarantined, failed oder not_updatable wird geloggt (ein Eintrag je Ablehnung).

### 5.5 Quarantäne-Text bei fehlendem Key-Feld
Kennt das Modul (`reported_boot_key` leer) oder die Release (`boot_key_hash` leer) keinen Key, laufen Quarantäne und Fehler wie bisher. Der angezeigte Grund und der Audit-Text bekommen dann den Zusatz: „Mögliche Ursache: Bootloader-Schlüssel passt nicht zur Release (z. B. Dev-Key) – Modul-Firmware zu alt, um das zu prüfen.“ Bei rolled_back bzw. Quarantäne nach rolled_back gilt das immer, bei transient nicht.

## 6. Audit-Trail (User-Entscheidung)

Alle Einträge gehen in `StationAuditLog` mit `station` und `module`. Geloggt wird **nur bei Zustandswechseln oder echten Ereignissen**, nie pro Heartbeat:

| Ereignis | Event (neu = *) |
|---|---|
| Anweisung ausgegeben | MODULE_FLASH_INSTRUCTED* |
| Phasenwechsel downloading/flashing/verifying | MODULE_FLASH_STARTED (erster) / MODULE_FLASH_PHASE* (weitere) |
| failed / rolled_back / rejected inkl. Text | bestehend |
| commit Erfolg | MODULE_FLASH_SUCCESS |
| commit-Mismatch / 409 | MODULE_FLASH_ROLLED_BACK bzw. MODULE_FLASH_CONFLICT* |
| N transiente Fehler → Fehlerzustand | MODULE_FLASH_GAVE_UP* |
| Quarantäne mit Grund | MODULE_QUARANTINED (Text um den Grund ergänzt) |
| → nicht updatebar / wieder updatebar | MODULE_NOT_UPDATABLE* / MODULE_UPDATABLE_AGAIN* |
| Retry durch User | MODULE_FLASH_RETRY* |

„Wartet“-Zustände werden **nicht** geloggt: Sie sind abgeleitet und flattern zusammen mit online/offline. Die Event-Namen sind Vorschläge, die Namensgebung entscheidet der Builder passend zu den bestehenden Choices.

## 7. Bootloader-Key-Erkennung (User-Entscheidung)

**Idee:** Ein laufendes MCUboot-Image wurde vom Bootloader akzeptiert. Der Key, mit dem das *laufende* Image signiert ist, ist also der Key, dem der Bootloader vertraut. Das gilt für die Konfiguration aus `sysbuild.conf`, wo MCUboot die Signatur validiert.

- **FW (FW-RemoteStation):** `identity` in `MODULE-DESCRIBE` bekommt das optionale Feld **`boot_key`**: die ersten 16 Hex-Zeichen (8 Byte) des SHA-256-KEYHASH, so wie ihn MCUboot im KEYHASH-TLV (Typ 0x01) des Images ablegt. `schema` bleibt 2, denn ein additives Feld ist abwärtskompatibel. Woher die FW den Wert nimmt, entscheidet der Builder. Empfehlung: zur Laufzeit aus dem TLV-Bereich des eigenen Images in slot0 lesen, dann ist der Wert garantiert identisch mit dem, was der Server aus dem Release-Artefakt liest. Alternative: zur Build-Zeit aus dem Signing-Key ableiten. Ist kein MCUboot vorhanden (Bare- oder Debug-Build) oder lässt sich der Wert nicht lesen, wird das Feld weggelassen.
- **Agent (`station_agent/slot_discovery.py`):** reicht `identity.boot_key` unverändert durch. Das passiert automatisch, wenn `identity` komplett durchgereicht wird; der Builder verifiziert das und ergänzt einen Test.
- **Server-Ingest:** `identity.boot_key` → `Module.reported_boot_key` (überschreiben, wenn vorhanden; leer lassen, wenn das Feld fehlt).
- **Server-Release-Import (`releases.py`):** KEYHASH-TLV aus dem `.signed.bin` parsen, dann Präfix → `boot_key_hash`. Fehlt der TLV, bleibt das Feld leer. Bestehende Releases bekommen den Wert per Backfill-Command/Datenmigration aus dem Storage, falls das billig ist; sonst bleiben sie leer und laufen weiter mit dem bisherigen Verhalten.
- **Vergleich:** Nur wenn beide Werte gesetzt sind. Mismatch → `not_updatable`, `reason_code=boot_key_mismatch`. Text:
  - passt `reported_boot_key` zum bekannten öffentlichen MCUboot-Dev-Key (Konstante im Code, aus `root-ec-p256.pem` berechnet): „Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (Dev-Key) – SWD-Reflash an der Bench nötig“
  - sonst: „Nicht updatebar: Bootloader-Schlüssel passt nicht zur Release (anderer Schlüssel) – SWD-Reflash an der Bench nötig“
- Das Modul-Detail zeigt den gemeldeten Key-Präfix (und „Dev-Key“, falls erkannt) als Info neben der FW-Version.

## 8. UI (frontend-design-Skill Pflicht)

- **Modul-Liste:** neue Spalte bzw. Pille mit dem Anzeige-Zustand, der Grund als Kurztext oder Tooltip.
- **Modul-Detail:** Pille + vollständiger Grund. Bei `failed` der Button „Erneut versuchen“, bei der Quarantäne der bestehende Freigabe-Button. Der Audit-Trail zeigt die neuen Events lesbar.
- **Station-Modulkarte** (`control/_module_card.html`, Alpine-getrieben mit Live-Client-State `isUpdating(...)`): Zustand und Phase. Die Integration in den bestehenden Alpine/WS-State statt eines reinen HTMX-Polls entscheidet der Builder. Der bestehende `firmware_updating`-Banner wird damit vereinheitlicht oder bleibt daneben konsistent; das entscheidet der Builder, es darf aber keine widersprüchlichen Anzeigen geben.
- **Live:** Modul-Detail und Modulkarte aktualisieren den Zustand per HTMX-Polling (Intervall nach bestehendem Muster, z. B. 5 s, solange ein Update läuft oder wartet). Die Liste reicht beim Reload.
- Bestehende Sichtbarkeits- und Rechte-Regeln bleiben unverändert. Wer das Modul sieht, sieht auch den Zustand; der Retry folgt den Rechten der Quarantäne-Freigabe.
- `{% comment %}`-Regel beachten. Alle Texte sind deutsch und für Laien verständlich.

## 9. API

- `ModuleSerializer` (`apps/api/module_firmware_serializers.py`) bekommt zusätzlich `update_status: {state, phase, reason_code, reason_text, since}` sowie `reported_boot_key`. Die Release-Serializer bekommen `boot_key_hash`.
- Der Filter auf `firmware_convergence` akzeptiert die neuen Werte.
- Das OpenAPI-Schema (drf-spectacular) dokumentiert die Enums für state, phase und reason_code.
- Die Retry-Action folgt §5.3.

## 10. Nicht-Ziele

- Kein Force-Button, kein Wartungsfenster, keine Bootloader-Updates im Feld.
- Keine Änderung an Drain- und Idle-Logik außer: N transiente Fehler → `failed`.
- Keine Änderung an der Target-Auflösung (Rollout-Gruppen, #169) und am Deployment-Flow (#170).
- **Kein FW-Release-Tag, kein linux-image-Release und kein SRCREV-Bump ohne ausdrückliche User-Freigabe.**
- „Wartet“-Zustände werden nicht persistiert und nicht auditiert.

## 11. Akzeptanzkriterien

1. Eine Station mit altem Agent (ohne `module_ota`-Capability) zeigt am Modul „Wartet: Station-Agent kann keine Module flashen (Image zu alt)“, nicht „updating“.
2. Fake-Agent-Test: Die Phasen downloading → flashing → verifying → commit erscheinen in UI und API und als einzelne Audit-Einträge (keine Duplikate bei wiederholten gleichen POSTs oder wiederholtem check-Polling).
3. Nach 5 transienten Fehlern: Zustand „Fehler: \<letzte Meldung\>“, `pending_updates` leer (Drain frei), ein Audit-Eintrag. „Erneut versuchen“ führt zurück auf „Update ausstehend“, ebenfalls mit Audit-Eintrag.
4. Modul meldet den Dev-Key, die Release ist prod-signiert: check liefert 204, kein Drain, Zustand „Nicht updatebar … (Dev-Key) …“, genau ein Audit-Eintrag, auch über viele Heartbeats.
5. Fehlendes `boot_key` (alte FW) → bisheriges Verhalten. Ein Quarantänetext nach rolled_back enthält den Key-Hinweis.
6. KEYHASH-Parsing ist gegen ein echtes, mit imgtool signiertes Test-Binary getestet (Dev-Key-Fixture).
7. Rechte und Sichtbarkeit unverändert (bestehende Tests grün). API + OpenAPI zeigen die neuen Felder und Werte.
8. FW: `module <id> describe` enthält `boot_key` auf einem MCUboot-Build. Auf der HIL-Bench verifiziert (HW-Disziplin gemäß Meta-CLAUDE.md), der Wert entspricht `imgtool`-KEYHASH des geflashten Images. Bare-Build ohne MCUboot lässt das Feld weg.
9. pytest grün, CI grün, review-loop bis 0. Squash-Merge nur nach User-Freigabe.

## 12. Reihenfolge und Lieferung

- **PR 1 – station-manager:** Spec + Plan + Server + Agent-Durchreichung + UI + API. Tolerant gegenüber fehlendem `boot_key`, deshalb unabhängig mergebar.
- **PR 2 – FW-RemoteStation:** `boot_key` in describe, parallel oder danach. Wirkt erst mit einer neuen FW auf den Modulen; der Release-Tag braucht die User-Freigabe.
- Der Agent-Teil wirkt im Feld erst mit einem linux-image-SRCREV-Bump, der ebenfalls die User-Freigabe braucht.

**Abschluss-Hinweis an den User:** Für ein Modul mit Dev-Key-Bootloader gibt es zwei Wege: das Target entfernen oder den Bootloader an der Bench per SWD mit dem Prod-Key reflashen.

## 13. Dem Builder überlassen

- Event-Namen und Choice-Werte im Detail, sofern sie konsistent mit dem Bestand sind.
- Mechanismus gegen doppelte INSTRUCTED-Einträge.
- N+1-freie Berechnung von `module_update_status` für Listen.
- FW-Quelle des `boot_key` (Laufzeit-TLV-Read empfohlen).
- Backfill von `boot_key_hash` für bestehende Releases (falls billig).
- Banner-Vereinheitlichung auf der Modulkarte.
