# Modul-Firmware-Update über den station-manager — Design

**Issue:** TBD (noch anzulegen)
**Datum:** 2026-09-12
**Status:** Design in Review (awaiting approval → implementation plan)

## Ziel

Firmware von **Modulen** einer Remote-Station (STM32-basiert, z. B. das
FM-Modul) **aus der Ferne aktualisieren** — analog zum bestehenden Linux-Image-OTA,
aber als **eigenständiger, vom Image entkoppelter Update-Pfad**. Der station-manager
ist Control Plane, Artefakt-Speicher und Betriebs-Cockpit; der `station_agent`
führt den physischen Flash aus; die Firmware bleibt dünn und selbstbeschreibend.

Sekundär, aber gleichrangig entworfen: **Module werden zu getrackten physischen
Objekten** (per eingebrannter STM32-UID), sodass der komplette Lebenszyklus und
die Firmware-Historie einem Modul folgen — unabhängig davon, in welcher Station /
welchem Slot es gerade steckt.

## Scope

**Im Scope:**
- Modul-Firmware als eigener, unabhängig versionierter Release-Stream.
- Deklaratives Soll-Zustands-Modell + Reconciler (selbstheilend), mit Rollout-Policy.
- Modul-Entität (UID-geführt) inkl. Lifecycle-Status und Slot-Zuordnung mit Historie.
- Fehler-Eindämmung (Quarantäne) gegen Update-Loops.
- Integration ins **bestehende** Audit-System (Station- **und** Modul-Sicht).
- Distribution über Server-Storage (Import + Pin), device-key-authentifiziert,
  HAMNET-tauglich.

**Bewusst NICHT im Scope (später nachrüstbar, ohne Umbau):**
- **Wartungsfenster** und **manueller Force-/Idle-Override** — nur als spätere
  Policy-Felder vorgesehen.
- **Feines Firmware↔PCB-Revisions-Gate** (`hw_version`) — für den Anfang nur
  Kompatibilität auf **Modul-Typ-Ebene**.
- **Batch/Delay-Rollout-Choreografie** über den einfachen Canary-Tag hinaus.
- Voll-Asset-/Beschaffungs-Verwaltung (das bleibt InvenTree; hier nur
  Fleet-Firmware-Betrieb).

## Kern-Architekturentscheidungen

Diese vier Achsen wurden im Brainstorming bewusst gegen die jeweilige Alternative
abgewogen und bilden das Fundament.

### 1. Modul-Firmware ist ein entkoppelter Stream (nicht ins Image gebündelt)

Modul-Firmware wird **unabhängig vom Linux-Image** bewegt und versioniert.

**Begründung:**
- **Unterschiedliche Änderungsraten:** ein SA818-/Audio-Fix hat nichts mit
  Kernel-/Agent-Updates zu tun. Kopplung zwänge jeden <1 MB-Firmware-Fix zu einem
  vollen Rootfs-A/B-Swap samt Stations-Reboot — absurd großer Blast-Radius.
- **Getrennte Fehler-/Rollback-Domänen:** ein kaputtes Modul-Update rollt *das
  Modul* zurück, nicht die Station.
- **Heterogene Fleets:** nicht jede Station ist gleich bestückt. Entkoppelt
  targetet man „alle Stationen mit Modultyp X in einem Slot".

Der einzige Vorteil der Kopplung (ein Mechanismus, weniger Infrastruktur) ist
Implementierungs-Komfort, kein Betriebs-Vorteil — und wird mit Kopplungsschmerz
bezahlt.

### 2. Deklaratives Soll-Zustands-Modell + Rollout-Policy (nicht imperative Kampagne)

Der Server hält die **Absicht** („Modultyp FM soll Version Y sein"); der Agent
meldet laufend das **Ist** und **konvergiert autonom**, sobald es sicher ist —
innerhalb einer server-seitigen Rollout-Policy.

**Begründung:**
- **Selbstheilend:** neue Station, im Feld getauschtes Modul, während eines
  Rollouts offline gewesene Station — alles gleicht sich ohne menschliches
  Nachtriggern an. Ein Kampagnen-Modell lässt genau diese Fälle durchs Raster
  fallen.
- **Passt zum Bestand:** der Heartbeat trägt ohnehin (bislang ungenutzt) die
  Modul-Versionen; Reconcile = „gemeldetes Ist gegen hinterlegtes Soll".
- **Risikoarm, weil lokal rückrollbar** (siehe Achse 4): Worst Case ist „Modul
  bleibt auf alter Version", nicht „Station bricked".

### 3. Safety-Gate: idle = Pflicht + Drain/Lock-out (kein Force, kein Fenster)

Der Agent flasht ein Modul nur, wenn es **idle** ist (kein aktives PTT, kein
aktiver Control-Lock / keine Bedien-Session). Sobald für ein Modul ein Update
**pending** ist, greift ein **Drain/Lock-out**: keine neuen Control-Sessions mehr,
damit ein Dauerbetrieb den Flash nicht aushungert.

- **Kein Force-Button:** Das Setzen der Soll-Version *ist* bereits der „so schnell
  wie sicher möglich"-Trigger (Drain → nächstes Idle → Konvergenz). Das Einzige,
  was ein Force zusätzlich könnte, wäre das Überschreiben der Idle-Bedingung
  (Reinflashen in ein laufendes QSO) — bewusst ausgeschlossen.
- **Kein Wartungsfenster** in dieser Iteration (späteres Policy-Feld).
- Dauerbetrieb-Stationen lösen sich über den Drain irgendwann auf; anhaltende
  Drift wird im Dashboard sichtbar (kein erzwungenes Flashen).

### 4. Trust: Defense in depth mit On-Device-Signaturprüfung als Wurzel

Die **unumgehbare** Echtheitsgarantie sitzt **im Modul** (Secure-Bootloader,
MCUboot-Klasse): der Bootloader verifiziert die Image-Signatur gegen einen fest
verbackenen Public Key und **bootet unsigniert/fremd-signiert nicht**. Darüber
liegt eine **Supply-Chain-Signatur** des Artefakts (Provenance + früher Reject
manipulierter Downloads).

**Begründung & Konsequenzen:**
- Der Weg Server → CM4 → USB → Modul ist an jedem Punkt angreifbar; nur das Modul
  selbst kann *wirklich* garantieren, dass legitime Firmware läuft. Ein
  kompromittierter Agent kann die Wurzel nicht umgehen.
- **Lokaler Rollback:** kaputte/nicht-selbstbestätigte Firmware → Bootloader
  revertiert automatisch auf die letzte gute Version. Rollback-Domäne = das Modul.
- **Key-Management ist einmalige Setup-Arbeit, kein Pro-Update-Schmerz:**
  Schlüsselpaar einmal erzeugen, Private Key sichern (Org-Secret), Public Key in
  den Bootloader bauen, Bootloader **einmal physisch pro Board** provisionieren
  (nicht übers Netz updatebar → Produktions-Key = langlebig, streng gehütet).
  Pro Update: CI signiert automatisch, Agent schiebt per USB.
- Der Agent braucht **nie** den Signing-Key — er transportiert nur ein bereits
  signiertes Image.

### 5. Distribution: Import + Pin auf Server-Storage (V3), Download nur über den station-manager

Der station-manager **importiert das Release-Artefakt und pinnt es auf eigenen
Storage** (wie beim Linux-Image-OTA). Der Agent lädt **ausschließlich über den
station-manager** (device-key-Auth), nie direkt von GitHub.

**Begründung:**
- **HAMNET-Stationen erreichen das offene Internet nicht** — ein Direkt-Download
  von GitHub ist damit ausgeschlossen. Der station-manager ist über VPN im HAMNET
  und der einzige verlässlich erreichbare Endpunkt.
- **Kein GitHub-Token auf Feld-Stationen** nötig; ein einziger Auth-/Netzpfad
  (über den bestehenden Tunnel).
- **Unveränderlichkeit:** ein auf GitHub gelöschtes/neu-gepushtes Release darf ein
  laufendes Rollout nicht brechen. Gepinnte Kopie = stabile Wahrheit.
- **Konsistenz** mit dem etablierten, funktionierenden Image-OTA — kein Neu-Erfinden.

## Schlüssel-Verwaltung (FW-Signing)

Es gibt **drei getrennte Schlüssel-Welten** — nur die erste ist neu und heikel:

| Schlüssel | Zweck | Wo | Geheim? |
|---|---|---|---|
| **FW-Image-Signing-Key** (ECDSA, MCUboot) | signiert das Firmware-Image; Modul-Bootloader verifiziert on-device | **nur Release-CI** | **ja, langlebig, streng** |
| **cosign** (keyless/OIDC) | Supply-Chain-Provenance der Artefakte | CI, kein Dauer-Key | nein (keyless) |
| **DeviceToken / Ed25519** | Agent↔Server-Auth pro Station | Server-DB + Station | (bestehend) |

**Kernregel:** Nur die **Release-CI** braucht den Private Key. Explizit **nicht**
der station-manager, **nicht** der Agent, **nicht** die Station, **nicht**
Entwickler (die bauen lokal mit dem nicht-geheimen MCUboot-Dev-Key, der nie ins
Feld geht).

Ablage:
- **Private Key** → **GitHub-Organization-Secret** auf `OE5XRX`-Ebene (alle
  HW-Modul-Release-Workflows teilen ihn, konsistent mit dem CI-Muster). Im Workflow
  zur Build-Zeit mit `umask 077` in eine Temp-Datei materialisiert, von `imgtool`
  genutzt, **nie committet, nie geloggt**.
- **Private *und* Public Key** → zusätzlich im **Team-Passwort-Manager** als
  sicheres Offline-Backup abgelegt.
- **Public Key** ist nicht geheim, wird in den Bootloader gebaut und **einmalig
  physisch pro Board** (SWD) provisioniert.

**Kritisch:** GitHub-Secrets sind nicht rücklesbar — geht der Key verloren, kann
niemand mehr Firmware signieren, die bereits ausgelieferte Bootloader akzeptieren;
Recovery nur per physischem SWD-Zugriff auf **jedes** Gerät. Daher das
Passwort-Manager-Backup und die Behandlung als langlebiger Schlüssel (Rotation =
Bootloader-Rebuild + Reflash aller Boards).

**Sicherheits-Eigenschaft:** Der geheime Signing-Key berührt station-manager und
die Stationen zu keinem Zeitpunkt. Selbst ein voll kompromittierter Server kann
keine gültige Firmware erzeugen.

## Datenmodell (station-manager)

> Offener Umsetzungspunkt: Es existiert bereits eine `apps/firmware`. Beim Planen
> klären, ob sie erweitert wird oder eine eigene App (`module_firmware`) sinnvoller
> ist. Reine Struktur-Frage, kein Architektur-Thema.

### Neue DB-Objekte — Übersicht
1. **`ModuleType`** — Registry flashbarer Modultypen.
2. **`Module`** — das physische, UID-getrackte Teil.
3. **`ModuleAssignmentHistory`** — dedizierte, abfragbare Zuordnungs-Historie.
4. **`ModuleFirmwareRelease`** — importiertes & gepinntes Artefakt.
5. **`ModuleFirmwareTarget`** — Soll-Zustand (eine Tabelle mit Scope-Diskriminator).
6. **`ModuleFirmwareConvergenceState`** — Reconciler-Buchhaltung (Versuche/Quarantäne).
7. **Erweiterung** von `StationModule` (`apps/control`) um eine FK auf `Module`.

> **Nicht** angelegt: keine „alle Board-Typen"-Tabelle (nur *firmware-tragende*
> Modultypen), **kein** `hw_version`/PCB-Revisions-Objekt (Achse c, zurückgestellt).

### `ModuleType` — Registry flashbarer Modultypen
Kleine Tabelle bekannter, flashbarer Modultypen (`fm`, `power`, `device-tester`, …);
`Module` und `ModuleFirmwareRelease` referenzieren per FK. **Eine Quelle der
Wahrheit** für gültige Typen, referenzielle Integrität fürs Typ-Kompatibilitäts-Gate,
Anker für spätere Typ-Metadaten (Anzeigename, zugehöriges HW-Repo). Ein Typ muss
registriert sein, bevor ein Modul dieses Typs erkannt wird. Enthält **nur**
Modultypen mit eigener flashbarer Firmware — reine PCBs (BusBoard, CM4-Carrier …)
tauchen hier nicht auf.

### `Module` — das physische, getrackte Objekt
- `uid` — die im STM32 fest eingebrannte 96-bit Unique Device ID. **Alleinige
  Identität.** Self-reported von der Firmware im Identity-Descriptor; kein
  Asset-Tag.
- `module_type` — **FK auf `ModuleType`**. Self-reported von der Firmware, gegen die
  Registry aufgelöst. Dient dem Typ-Kompatibilitäts-Gate (FM-FW niemals auf ein
  Power-Modul).
- `lifecycle_status` — **physischer** Lifecycle, z. B. `ready` (einsatzbereit /
  Lager) · `deployed` (in einer Station aktiv) · `defect` · `in_lab` (zur Analyse)
  · `retired`. (Finale Liste beim Implementieren.)
- **Firmware-Konvergenz-Zustand** — **orthogonales** Feld, *kein* Lifecycle:
  `ok` · `updating` · `quarantined`. Ein Modul kann gleichzeitig `deployed` und
  `quarantined` sein.

> Bewusst weggelassen (Achse c): `hw_version` (PCB-Revision). Später als
> zusätzliches Feld + feineres Kompatibilitäts-Gate nachrüstbar.

### Slot-Zuordnung + `ModuleAssignmentHistory`
**Aktuelle** Zuordnung: das heutige `StationModule` (`apps/control`, hält
`slot`/`type`/`version`) wird um eine **FK auf `Module`** erweitert — aus
„entdeckter Slot-Inhalt" wird „welches getrackte Modul steckt jetzt in (Station,
Slot)".

**Verlauf:** eine **dedizierte `ModuleAssignmentHistory`-Tabelle** (Modul, Station,
Slot, von–bis) — direkt abfragbar („wo war #UID wann"), nicht aus Audit-Events
rekonstruiert. Firmware-Historie hängt am **Modul**, nicht am Slot.

**Auto-Swap-Erkennung:** meldet der Agent im Inventory in einem Slot eine neue UID,
erkennt der Server den Modultausch → neuer `ModuleAssignmentHistory`-Eintrag +
Audit-Event, ohne manuelles Ummelden.

### `ModuleFirmwareRelease` — importiertes & gepinntes Artefakt
- `module_type`, `version`
- `storage_key` (gepinntes Binary auf Server-Storage), `sha256`
- Supply-Chain-Signatur-Metadaten
- Quell-Release (GitHub-Tag/URL) für Provenance
- `imported_at`, `archived_at` (soft delete, wie bei `ImageRelease`)

### `ModuleFirmwareTarget` — der Soll-Zustand
- pro `module_type` (FK auf `ModuleType`)
- Scope-Hierarchie: **Fleet-Default → Tag-Override → Station-Override**
- Policy-Feld: **Canary-Tag zuerst** (bewusst simpel gehalten)
- Auflösung: effektive Soll-Version pro (Station, Modultyp), abgeglichen gegen die
  vom Modul gemeldete Ist-Version.

### `ModuleFirmwareConvergenceState` — Reconciler-Buchhaltung
Pro (`Module`, Ziel-`ModuleFirmwareRelease`): `attempts`, Zustand
(`ok`/`updating`/`quarantined`), letzter Fehler(-modus), Zeitstempel. Trägt die
Quarantäne-Logik (siehe Fehler-Eindämmung). Die Quarantäne ist damit sauber an das
Tupel (Modul, Ziel-Version) gebunden — eine neue Ziel-Version ist ein neuer
Datensatz und wird automatisch wieder probiert.

### Audit — kein neues System
Alle Ereignisse laufen über das **bestehende** Audit-System (`apps/audit` /
`StationAuditLog`, propagiert per Signals über App-Grenzen). Neu: die **UID ist ein
auditierbares Subjekt**, sodass zwei Sichten möglich sind:
- **Station-zentriert:** „was ist an OE5XRX passiert".
- **Modul-zentriert:** „komplette Lebensgeschichte von Modul UID=…", die dem
  physischen Teil folgt.

Auditierte Ereignisse: Soll-Zustand-Änderungen (wer setzt welche Ziel-Version),
Konvergenz-Ereignisse (flash gestartet/erfolgreich/rolled_back/rejected/failed),
Quarantäne, Modul-Swaps.

## Reconciler-Flow (server-seitig gerechnet)

Der **Server** rechnet Ist/Soll ab (Policy gehört auf den Server, nicht in den
Agent). Über denselben Poll-Kanal wie das Image-OTA (`check`-artig) erhält der
Agent die effektive Anweisung: „Slot X / Modultyp FM soll auf Version Y — hier ist
der Download". Der Agent gehorcht und meldet Status
(`downloading → flashing → verifying → success/rolled_back/failed`) + `commit`.
Maximal konsistent mit dem bestehenden Deployment-check/status/commit-Muster.

**Kompatibilitäts-Gate (Typ-Ebene):** Vor dem Flashen prüft der Reconciler, dass
`ModuleFirmwareRelease.module_type` zum gemeldeten `Module.module_type` passt.
Mismatch → nicht flashen.

## Agent-Ausführung (`station_agent`)

- Neues `module_ota.py`, spiegelt die bewährten `ota.py`-Muster (resumable Download
  + SHA-256-Verify), Download-Quelle = station-manager-Endpoint.
- Ablauf pro Modul: Drift erkannt → **Safety-Gate (idle)** → **Drain** (keine neuen
  Control-Sessions) → Download → **DFU-Flash** → Reset → Version verifizieren →
  Stabilität abwarten (Health-Gate-Deadline) → Ergebnis melden.
- **Crash-sicher / idempotent:** der Agent leitet den Zustand **immer** aus der real
  gemeldeten Modul-Version ab, nie aus lokalen Flags. Reboot/Absturz mitten im
  Flash → beim Hochfahren wird die tatsächliche Version gelesen und von der Realität
  aus neu abgeglichen.
- **Heartbeat:** die Modul-Inventory-Liste (Typ + Version + UID je Slot) wird
  endlich verdrahtet (bislang Platzhalter).

## Fehler-Eindämmung — kein Update-Loop, kein Bricking

**Bricking ist bereits ausgeschlossen** durch den Secure-Bootloader (Achse 4):
kaputtes Image → Auto-Rollback auf letzte gute Version. Worst Case = „funktionsfähig
auf alter Version".

**Gegen Update-Loops** eine explizite Eindämmung im Reconciler:
- **Versuchszähler pro (Modul, Ziel-Version).** Nach **N** Fehlversuchen
  (Default **3**) → **Quarantäne** für dieses Ziel; keine automatischen Retries mehr.
- **Quarantäne ist pro (Modul, Ziel-Version)**, nicht global: eine spätere
  Fix-Release ist ein *neues* Ziel → wird automatisch probiert, ohne dass jedes
  Modul von Hand „entklebt" werden muss.
- **Fehlermodi unterschieden:**
  - **Rejected** (Version ändert sich nie → fremde Signatur / Downgrade):
    **sofort Quarantäne, kein Retry** (Retry hilft prinzipiell nicht).
  - **Rolled_back** (geflasht, aber Health-Gate failed → revertiert): 1–2 Retries
    erlaubt (evtl. transient), dann Quarantäne.
  - **Transient** (Download-Abbruch, Station offline, Modul busy): normaler Retry
    mit Backoff, zählt **nicht** aufs Quarantäne-Limit.
- **Laut sichtbar:** Quarantäne → Audit-Event + Dashboard-Warnung. Das ist der
  Trigger für den Modultausch (Betriebs-Kreis unten).

## Geschlossener Betriebs-Kreis

Fehler → **Quarantäne** (Fehler-Eindämmung) → **Audit-Event + Dashboard-Warnung**
(Audit) → **Modultausch** #alt → #neu mit Zuordnungs-Historie (Modul-Entität) →
defektes Modul geht `in_lab`, Lebensgeschichte folgt der UID. Alle drei
Sub-Systeme greifen ineinander.

## Repo-übergreifende Abhängigkeiten

- **`FW-RemoteStation`:** Der Release-Workflow muss **signierte, DFU-fähige**
  Artefakte produzieren (Secure-Bootloader + Produktions-Key), nicht das bare/
  unsignierte Binary. Schlüssel-Handhabung siehe Abschnitt *Schlüssel-Verwaltung*
  (Private Key = Org-Secret **und** Passwort-Manager-Backup; Public Key ins Board
  provisioniert, einmalig physisch). Firmware exponiert im Identity-Descriptor:
  **UID + Modultyp + Version**.
- **`linux-image`:** Ein DFU-Flash-Tool (`dfu-util` bzw. äquivalent) muss ins
  Yocto-Image aufgenommen werden.
- **`station-manager`:** neue Modelle/Views/API + Reconciler + Audit-Erweiterung +
  Dashboard-Sichtbarkeit (siehe oben).

## Offene Implementierungspunkte (kein Architektur-Risiko)

1. `apps/firmware` erweitern vs. neue App `module_firmware`.
2. Herkunft der `hw_version` (falls später gewünscht): HW-Strap/Resistor-ID,
   NVS-provisioniert oder build-time — bewusst zurückgestellt.
3. Exakte Endpoint-Shapes des Reconcile-Kanals (check/status/commit für Module).
4. Konkretes DFU-Tooling im Image + Udev-/Slot-Mapping des Moduls für den Flash.
5. Finale Enum-Liste für `lifecycle_status`.

## Warum das so und nicht anders (Zusammenfassung der Trade-offs)

- **Entkoppelt statt gebündelt** — verschiedene Änderungsraten, Blast-Radien,
  heterogene Fleets.
- **Deklarativ statt imperativ** — selbstheilend, passt zum vorhandenen Heartbeat,
  risikoarm durch lokalen Rollback.
- **On-Device-Trust-Wurzel** — die einzige Stelle mit echter Garantie; Agent-
  Kompromittierung kann sie nicht umgehen.
- **Import+Pin statt Direkt-Link** — HAMNET-Erreichbarkeit, kein Token im Feld,
  Unveränderlichkeit, Konsistenz mit Image-OTA.
- **Modul als UID-getracktes Objekt** — macht Diagnose/Modultausch/Historie real,
  ohne in Voll-Asset-Management auszuufern.

---

## Decomposition & Status (Programm-Ebene)

Zerlegt in Teilbereiche, jeder mit eigenem Spec → Plan → PR-Zyklus (Kind-Session-Muster
aus der globalen CLAUDE.md).

| # | Teilbereich | Repo | Status |
|---|---|---|---|
| Overview | Strategie/Architektur (dieses Dokument) | station-manager | dieses Doc |
| **E** | FW-Produktions-Signing-Key & Provisioning | FW-RemoteStation | ✅ **gemerged** (PR #64, `main`); Bench/Task 7 (HIL) offen |
| **A** | Modul-Inventar & Identität | station-manager | **NEXT** — Fundament |
| **B** | Firmware-Release-Management (Import + Pin + Serve) | station-manager | backlog (parallel zu A) |
| **C** | Desired-State & Reconciler (Target/Quarantäne/API) | station-manager | backlog (nach A+B) |
| **D** | Agent-Flash-Ausführung (DFU + sim-Backend) | station_agent + linux-image | backlog (nach C; braucht E-Artefakte) |

Sequenz: A (Fundament) ∥ B → C → D. E ist unabhängig fertig.

## Teilbereich A — Inputs & offene Fragen (für die nächste Session)

**Onboarding-Entscheidung (unbekannte UID im Feld) — ENTSCHIEDEN in Teilbereich A: (ii).**
- (i) **TOFU** — auto-anlegen als `deployed`, kein Bench-Pre-Registrieren.
- (ii) **auto-anlegen + flaggen** ✅ **gewählt** — erkannt, aber `unregistered` bis ein Admin bestätigt. Lifecycle wird orthogonal aus der Zuordnung abgeleitet (`deployed`/`ready`), `registration_status` bleibt `unregistered` bis Staff bestätigt (`services.confirm_registration`).
- (iii) **strikt** — nur bench-registrierte UIDs akzeptiert; unbekannte geflaggt + von Updates ausgeschlossen.
→ **Umgesetzt in A** (Spec `2026-09-13-module-firmware-A-inventory-identity-design.md`). B/C bauen darauf auf.

**native_sim / Sim-Station — Identität & „Update" (Input für A + D):**
Sim hat kein MCUboot/DFU/keine STM32-UID. Konsequenzen:
- **Identität:** `Module.uid` ist ein opaker String. Sim erzeugt beim ersten Start eine
  **synthetische, persistierte UID** (im schreibbaren Bereich) und meldet sie wie eine HW-UID.
  Neues Feld **`uid_source`** (`stm32_uid` | `synthetic`) unterscheidet fürs Audit.
- **„Flashen" am Sim:** kein DFU → **Datei-Swap** des `native_sim`-Binaries in einem
  **schreibbaren Bereich** des qemu-Images + Service-Restart (idealerweise Mini-A/B für lokalen
  Rollback). Erfordert linux-image-Anpassung (schreibbare Modul-Partition + Sim-Modul-Service).
- **Testbarkeit:** A–C (Inventar/Desired-State/Reconciler) am Sim voll testbar; D (physisches
  Flashen) real-HW-only, am Sim via **Flash-Executor-Backend `sim`** (no-op/Datei-Swap).
- **Trust-Ehrlichkeit:** Sim hat KEINE Hardware-Trust-Wurzel — Verify ist best-effort (Test-/Dev-Station).

**E→A-Übergabe:** E's `provision.sh` erzeugt `provision-<uid>.json`
(`{uid, module_type, pyocd_target, firmware_version, signed_app, mcuboot_hex, provisioned_at}`) —
das ist der Registrierungs-Input, den A konsumiert.
