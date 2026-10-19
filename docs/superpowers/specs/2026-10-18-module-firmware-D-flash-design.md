# Teilbereich D — Modul-Firmware flashen (Agent + Image + Server-Rest) — Design

Stand 2026-10-18. Baut auf dem Overview `station-manager/docs/superpowers/specs/2026-09-12-module-firmware-update-design.md`
und den gemergten Teilbereichen A/B/C/E auf.

> **Vorrang:** Bei Widerspruch gilt der Abschnitt „Klassifikation & Fehlerfälle" (überschreibt Schritt 7
> „unverändert → rejected" und Schritt 9 „nie blind neu flashen" für den DFU-Hänger-Fall). Für den
> Sim-Flash ist der linux-image-Vertrag `docs/module-firmware-flash.md` verbindlich (statt der groben
> Beschreibung in Schritt 6).

## Absicht

Ein Modul (heute: FM-Transceiver, fm_board/STM32U575) soll seine Firmware **ohne Menschen an
der Station** bekommen: Soll-Version im station-manager setzen → der Agent der Station
konvergiert selbst, sobald das Modul frei ist → Ergebnis sichtbar. Dieselbe Kette muss auf der
**qemu-Sim-Station** laufen, damit der ganze Ablauf ohne Hardware testbar ist (User-Entscheid).

## Ziele

- Agent führt die Reconcile-Anweisungen (`reconcile/check|status|commit`, aus C) tatsächlich aus:
  Download → Flash → Verify → Ergebnis melden.
- Echte HW: USB-DFU (`dfu-util`) + MCUboot-Swap/Rollback. Sim: Binary-Tausch + Harness-Restart.
- Während eines Flashs zeigt die Oberfläche **„Firmware-Update läuft"** für das Modul und sperrt
  die Bedienung; das USB-Verschwinden/Wiederauftauchen erzeugt **keine** „Modul weg/neu"-Effekte
  (kein Karten-Flackern, keine Inventar-Audits).
- Kein Flash in einen laufenden Betrieb: Idle-Gate + Drain.
- Crash-/Reboot-sicher: Zustand immer aus der real gemeldeten Modul-Version ableiten.

## Nicht-Ziele

- Kein Force-Button, kein Wartungsfenster (Overview §3 — bewusst).
- Kein Bootloader-Update (MCUboot ist nicht feldupdatebar).
- Keine neuen Modultypen außer FM; Mechanik aber generisch pro `module_type` halten.
- Keine weitere GUI über das hinaus, was hier explizit steht (Rest macht das GUI-PR #166).

## Anti-Ziele (wäre ein Fehlschlag, auch wenn es „funktioniert")

- Flash startet, während jemand funkt / PTT aktiv / Audio streamt.
- Ein Modul landet in einem Update-Loop (endlos flashen) — Quarantäne aus C muss greifen.
- Agent-Neustart mitten im Flash hinterlässt einen „hängenden" updating-Zustand oder flasht doppelt.
- Zwei Module mit gleicher VID:PID werden verwechselt (falsches Modul geflasht).

## Fakten aus dem Bestand (Recon 2026-10-18, origin/main)

- **Server-API (C) ist fertig:** `POST /api/v1/module-firmware/reconcile/check/` → 204 oder
  `{convergence_id, module_uid, slot, module_type, variant, target_version, download_url (relativ),
  checksum_sha256, size_bytes}`. `…/<id>/status/` mit `downloading|flashing|verifying|failed|rolled_back|rejected`
  (`failed`=transient, zählt nicht; `rolled_back` zählt, Quarantäne bei 3; `rejected` sofort Quarantäne).
  `reconcile/commit/` `{convergence_id, version}` → 200 bei Soll-Version, 409 bei Mismatch (= rolled_back).
  DeviceKey-Auth wie Image-OTA (Ed25519-signierte Requests über `HttpClient`).
- **Agent:** Hauptthread = Heartbeat-Loop (60 s), alle `ota_check_interval` Heartbeats `_handle_ota()`.
  `ota.download_firmware_resumable(...)` + SHA-256 wiederverwendbar. Slot-Erkennung über
  `/dev/oe5xrx/slotN/control` (udev, nach Hub-Port `1-1.N`), Single-Owner-`control_device_lock`,
  Rediscovery alle 30 s mit Debouncer (Slot fällt nach 2 Misses raus → Inventar-Frame → Server).
  Kein lokales „busy"-API; PTT-Zustand in `broker._ptt`, Audio in `AudioClient`.
- **ControlLock ist rein server-seitig** (`apps/control/lock.py`, praktisch scope „station" = ganze Station).
  Der Agent erfährt Lock-Vergaben nicht.
- **Control-WS:** Agent→Server `event`-Frames werden an Browser durchgereicht; Server→Agent kennt
  `command/subscribe/unsubscribe/ptt_keepalive`, Unbekanntes wird ignoriert.
- **FW:** VID:PID `2fe3:0012` in Runtime **und** DFU-Modus; DFU-Modus hat nur das DFU-Interface (kein
  CDC → Control-Symlink verschwindet); Serial im DFU-Modus vermutlich anders/fehlend → **Auswahl per
  USB-Port-Pfad (`dfu-util -p 1-1.N`)**. Eintritt nur per DFU_DETACH (`dfu-util -e`). Nach Download
  Self-Reboot nach 500 ms → MCUboot-Swap; Health-Gate 30 s, sonst Revert. `module <id> describe`
  meldet `identity.version` = `YY.MM.DD-NN`.
- **Referenz-Implementierung:** `FW-HIL/src/fw_hil/backends/live_dfu.py` + `fw_hil/dfu.py`
  (detach → 3 s → download → 45 s settle → Version lesen → klassifizieren) — Logik übernehmen, um `-p` erweitern.
- **Image:** read-only rootfs, `/var` = `/mnt/data/var` (beschreibbar). Agent läuft als root.
  `dfu-util` jetzt im Image (linux-image #102), Slot→Port in `/etc/oe5xrx/slots.conf`.
  Sim: `oe5xrx-sim-harness.service` + Override `/var/lib/oe5xrx/sim/native-sim-fm` (#102).
- **Release-Assets** (FW-Release 26.10.18-01): `fm-sa818-{vhf,uhf}.signed.bin`, `.native_sim`, je `.bundle`,
  `SHA256SUMS`. Der Import (B) zieht heute **nur** `.signed.bin`.

## Ablauf (entschieden)

1. **Trigger = Soll-Version** (C). Agent fragt `reconcile/check` im OTA-Takt; zusätzlich ein
   **Server→Agent-Nudge** `firmware_check` über die Control-WS, sobald sich für die Station ein Ziel
   ändert (kein Force — nur „jetzt nachsehen").
2. **Drain (Server):** Sobald für ein Modul der Station ein Update ansteht (Drift + nicht quarantined),
   verweigert `lock.acquire/request/transfer` **neue** Lock-Vergaben für diese Station mit klarer
   Meldung („Firmware-Update ausstehend"). Eine laufende Session wird nicht unterbrochen.
3. **Idle-Gate:** Server liefert eine Anweisung nur, wenn für die Station **kein ControlLock gehalten**
   wird; der Agent prüft zusätzlich lokal: kein PTT, kein aktiver TX-/Mic- oder Diag-Audio-Stream für den Slot (reine RX-Streams zählen
   bewusst **nicht** — jeder Viewer abonniert RX ohne Lock; RX fällt während des Flashs einfach aus). Nicht idle →
   kein Status (oder `failed` transient) → nächster Zyklus.
4. **Pause des Slots (Agent):** Slot in eine geteilte „paused/flashing"-Registry eintragen. Heartbeat-
   Inventar, Broker-Execute, Telemetrie-Polls und der Rediscovery-Debouncer respektieren sie
   (überspringen statt blockieren; der Slot wird im Inventar als **`updating`** weitergemeldet statt zu
   verschwinden). Agent sendet `event` „firmware_update_started/finished" → Modul-Karte zeigt
   „Firmware-Update läuft" + gesperrt.
5. **Download** `download_url` (über `HttpClient`, resumable) nach `/var/lib/station-agent/module-fw/`,
   SHA-256 prüfen → Status `downloading`.
6. **Flash (Backend `dfu`):** Status `flashing`; `dfu-util -e -p <port>` → kurz warten →
   `dfu-util -a 0 -p <port> -D <file>.signed.bin`. Port-Pfad aus `/etc/oe5xrx/slots.conf`.
   **Flash (Backend `sim`):** gemäß linux-image `docs/module-firmware-flash.md` (.new → hardlink .prev →
   pending → rename → restart → health → rm pending).
7. **Verify:** Status `verifying`; warten bis der Control-Symlink zurück ist, dann über die gesperrte
   Verbindung `module <id> describe` pollen bis die Settle-Zeit (≈ 45 s nach Download, siehe HIL) vorbei
   ist → Klassifikation siehe „Klassifikation & Fehlerfälle" (Version == Ziel → `commit`; alte Version
   zurück / nicht zurück in **3 min** → `rolled_back`).
8. **Slot freigeben**, frisches Inventar senden (der Server leitet den Endzustand aus
   `last_reported_version` ab), `event` „finished".
9. **Crash-Sicherheit:** Agent persistiert nur „Flash für convergence_id X auf Slot N läuft seit T" in
   `state_dir`. Beim Start: Marker vorhanden → Slot-Version lesen → wie Schritt 7 klassifizieren und
   melden (Ausnahme DFU-Hänger, siehe unten). C's Resume (check liefert dieselbe `updating`-Row) deckt den Rest.
10. **Ein Flash pro Station gleichzeitig.**

## Klassifikation & Fehlerfälle (Calls des Koordinators nach Builder-Check)

- **`rolled_back` = „Modul kommt mit der alten Version zurück"** — egal ob die Trial-Version kurz
  sichtbar war. Die Trial-Phase ist auf echter HW (Health-Gate 30 s, CDC evtl. nie oben) und im Sim nicht
  zuverlässig beobachtbar; ein „rejected"-Raten würde jede Revert sofort quarantänen und das
  3-Versuche-Limit aushebeln. **`rejected`** nur bei eindeutiger Ablehnung **vor** dem Flash-Effekt:
  Artefakt passt lokal nicht (Modultyp/Variante des Slots ≠ Anweisung) oder `dfu-util` meldet eine
  Ablehnung durch das Gerät. SHA-Mismatch nach Download = `failed` (transient, neu laden).
- **DFU bricht mittendrin ab** (dfu-util-Fehler, Agent-Crash zwischen detach und download): Die FW
  verlässt den DFU-Modus nicht von selbst. Der Agent **darf** den Download erneut ausführen, solange das
  Gerät am selben Port im DFU-Modus hängt (max. 2 Wiederholungen, auch nach Neustart via Crash-Marker) —
  das ist kein „blindes Neuflashen", weil das Modul ohnehin nicht läuft. Meldet `failed` (transient).
  Bleibt es danach hängen → `rolled_back` + Event „Modul hängt im DFU-Modus — Power-Cycle nötig"
  (Dashboard sichtbar).
- **Drain nur, wenn Konvergenz möglich ist:** Drain greift ausschließlich, wenn (a) der Agent die
  Fähigkeit `module_ota` (mit Backend `dfu`/`sim`) im Heartbeat/Inventar meldet, (b) für das Modul ein
  passendes Artefakt existiert (für Sim: `.native_sim`), (c) die Station online ist. Sonst kein Drain —
  alte Agents/fehlende Sim-Artefakte sperren nie eine Station.

## User-Entscheidungen (2026-10-18)

- **Drain/Sperre gilt für die ganze Station** (ControlLock bleibt station-scoped, kein Umbau auf
  pro-Modul-Locks).
- **Keine Höchstdauer für den Drain:** die Sperre bleibt, bis das Update durch ist (bzw. Quarantäne /
  Ziel entfernt). Konsistent mit Overview §3 (kein Force, kein Fenster). Sichtbarkeit im UI/Dashboard
  („Station wartet auf Firmware-Update seit …") ist deshalb Pflicht.
- Sim-Backend ist Teil dieser Runde (zum Testen ohne HW).

## Server-Änderungen (station-manager, klein)

- Drain-Regel in `apps/control/lock.py` (+ Tests), Idle-Bedingung in `ReconcileCheckView`.
- Nudge-Frame `firmware_check` an die Agent-Gruppe bei Target-/Release-Änderung.
- Modul-Karte/Control-UI: Zustand „Firmware-Update läuft" aus `event` bzw. Inventar-`updating`.
- **Sim-Artefakt:** Import (B) zieht zusätzlich `{prefix}-{variant}.native_sim` (+ bundle, cosign/SHA wie
  `.signed.bin`, optional — fehlt es, kein Fehler). `check` liefert für Module mit
  `uid_source=synthetic` die Sim-Datei (zweiter Download-Endpoint oder `?artifact=sim`) und ein
  `artifact`-Feld (`dfu`|`sim`). Echte HW bekommt nie ein Sim-Artefakt und umgekehrt.

## Image-Änderungen (linux-image) — ERLEDIGT in linux-image #102 (gemergt)

dfu-util, `/etc/oe5xrx/slots.conf`, Sim-Override + Vertrag `docs/module-firmware-flash.md`. Offen:
Agent-SRCREV-Bump (nach diesem PR), FW_RELEASE_TAG-Bump läuft separat.

## Abnahme

- Unit/Integration im station-manager (pytest) für Drain, Idle-Bedingung, Nudge, Sim-Artefakt.
- Agent-Tests mit Fake-`dfu-util` und Fake-Slot (Klassifikation success/rejected/rolled_back/timeout,
  Crash-Marker-Recovery, Pause-Registry verhindert Inventar-Drop).
- **E2E auf der Sim-Station (qemu)** und **Bench (echte HW)**: NICHT in diesem PR (separat danach).

## Dem Builder überlassen

Exakte Frame-Namen/Felder, Polling-/Settle-Konstanten (Startwerte oben), Struktur der Pause-Registry,
Datei-Layout unter `/var/lib`, ob der Nudge über `event`-Gruppe oder eigenen Typ läuft.
