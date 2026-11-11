# Generische Metriken + konfigurierbare Schwellwert-Alarme (inkl. PowerBoard)

Datum: 2026-11-03 · Branch `feature/generic-metrics` · ein PR (Spec + Plan + Code) in `station-manager`
Begleit-PR in `linux-image` (INA226-Overlay + Agent-Bump), siehe §8.

## Warum

Das PowerBoard misst Strom und Spannung der 12-V- und 5-V-Schiene (2× INA226).
Die Werte sollen über den Agent an den Server und dort angezeigt und alarmiert
werden.

Heute kostet **jeder** neue Messwert: Spalte in `StationTelemetry` + Migration +
Mapping in `apps/stations/ingest.py` + neuer `AlertRule.AlertType` + eigene,
fast identische `_check_*`-Funktion in `apps/monitoring/engine.py` + Serializer +
Template. Das PowerBoard allein wären 8+ Werte. Die Telemetrie wächst weiter —
deshalb wird der Messwert-Pfad **jetzt** generisch, und alles, was sinnvoll
passt, wird gleich mit umgestellt (User-Entscheidung: nicht warten).

## Ziele

1. Ein generischer `metrics`-Block im Heartbeat, vom Agent selbst beschrieben;
   neue Messwerte brauchen **keine Server-Änderung** (kein Code, keine Migration).
2. Generische Schwellwert-Alarmregeln mit **Mindestdauer**, vollständig in der
   UI (und API) konfigurierbar — nichts davon fest im Code außer Startwerten.
3. Bestehende reine Messwert-Alarme (CPU-Temp, Disk, RAM) laufen über den
   generischen Pfad; Verhalten und Schwellen bleiben für den Betrieb gleich.
4. Neue Metriken (§4) inkl. PowerBoard werden erfasst, angezeigt, alarmierbar.
5. Die spätere Historie (Spec `2026-10-01-station-telemetry-history-design.md`,
   eigenes Paket **danach**) kann ohne Umbau unter denselben `metric_key`s
   mitschreiben.

## Nicht-Ziele

- **Keine Historie/Zeitreihen-Tabelle** in diesem PR (kommt danach). Hier nur
  „aktueller Wert" + Alarm-Zustand.
- **Keine Per-Station-/Per-Region-Schwellen.** Regeln gelten global (User-Entscheidung).
- **Kein Umbau der Ereignis-Alarme** mit eigener Zustandslogik: `station_offline`,
  `ota_failed`, `unexpected_reboot`, `power_warning` (vcgencmd-Bits, einmal pro
  Boot), `storage_health` (Verschleiß + io-error-Baseline). Die bleiben typisiert.
- Optionale Metriken bewusst draußen (User): NTP-Sync, Agent-RSS, PTT-Sendezeit,
  Audio-Link-Qualität über generische Metriken.
- Kein Alarm-Hardware-Interrupt (INA226 ALERT-Pins sind nicht verdrahtet).

## Anti-Ziele (wäre ein Fehlschlag, auch wenn es „funktioniert")

- Alarm-Spam: ein kurzer Spannungseinbruch beim Senden darf keinen Alarm auslösen;
  Alarme dürfen nicht flattern.
- Ein Agent-Bug (z. B. Keys mit Zufallsanteil) darf DB oder UI nicht fluten.
- Bestehende Stationen mit altem Agent verlieren ihre CPU/Disk/RAM-Alarme.
- Heartbeat bricht wegen fehlerhafter Metrik-Daten (Ingest-Fehler isolieren, wie
  heute bei `ingest_telemetry`).

## 1. Hardware-Fakten (verifiziert)

- 2× INA226, Shunt 10 mΩ, Messbereich ≈ ±8,19 A. **+12 V = `0x41`, +5 V = `0x40`.**
- I²C `I2C_SCL1`/`I2C_SDA1` = CM4-Stecker J201 Pin 56/58 = GPIO3/GPIO2 → Linux `i2c-1`.
  Der Bus ist **Punkt-zu-Punkt CM4 ↔ PowerBoard**, kein anderes Modul hängt dran.
- Keine ALERT-Pins → Polling.

## 2. Heartbeat-Kontrakt: `metrics`-Block

Neues optionales Top-Level-Feld im Heartbeat-Payload (neben `inventory`, `telemetry`):

```json
"metrics": {
  "cpu.temp_c": 52.1,
  "cpu.usage_pct": 7.3,
  "pwr.12v.voltage_v": {"min": 11.82, "max": 12.31, "avg": 12.10},
  "net.eth0.rx_kbps": 12.4
}
```

- Key-Konvention `domain[.instance].name_unit`, Regex
  `^[a-z0-9_]+(\.[a-z0-9_]+){1,3}$`, max. 64 Zeichen.
- **Mount → Key-Instanz** (Agent und Server-Fallback identisch, eine gemeinsame
  Funktion): `/` → `root`, sonst führenden `/` entfernen, `/` → `_`, alles außer
  `[a-z0-9_]` → `_`, lowercase (z. B. `/boot` → `boot`, `/data/log` → `data_log`).
  Interfaces analog (`eth0`, `wlan0`). Diese Keys sind stabil (Historie baut darauf).
- Wert = Zahl **oder** `{"min","max","avg"}` (Zahlen; über das Heartbeat-Fenster).
  Skalare Werte werden serverseitig als `min=max=avg=value` behandelt.
- Fehlt eine Quelle (qemu, kein PowerBoard, kein Interface): Key weglassen, nie `null`/0.
- Max. 200 Keys pro Heartbeat; Überschuss wird verworfen + geloggt.

### 2.1 Selbstbeschreibung durch den Agent (`metric_descriptors`)

Der Agent beschreibt seine Metriken selbst — **ein neuer Messwert ist damit nur
noch eine Agent-Änderung, der Server wird nie angefasst** (User-Entscheidung).

- Jeder Heartbeat trägt `metric_descriptors_hash` = SHA-256 (hex) über die
  kanonische Serialisierung `json.dumps(sorted(descriptors, key=lambda d: d["key"]),
  sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. Der Server rechnet
  beim Empfang einer vollen Liste mit derselben Funktion nach und speichert den Hash
  **pro Station**; weicht der gesendete Hash vom gespeicherten der Station ab, antwortet
  er mit `metric_descriptors_required`. Jede Station lädt ihre Liste also einmal hoch.
- Die volle Liste `metric_descriptors` wird mitgeschickt beim ersten Heartbeat nach
  Agent-Start, wenn sich der Hash ändert, und wenn die Heartbeat-Antwort
  `"metric_descriptors_required": true` enthält (Server kennt den Hash nicht,
  z. B. nach DB-Reset).
- Descriptor pro **konkretem** Key:
  ```json
  {"key": "pwr.12v.voltage_v", "label": "12 V rail voltage", "unit": "V",
   "group": "power", "precision": 2, "min": 0, "max": 30}
  ```
  `group` aus fester Menge (`system`, `disk`, `network`, `power`, `module`, `other`;
  Unbekanntes → `other`). `label` ≤ 64 Zeichen, Englisch. `min`/`max` = plausibler
  Bereich, optional.
- Einheit ist Teil des Key-Namens (`_v`, `_pct`, `_kbps`) — eine Einheitenänderung
  ist damit immer ein neuer Key; bestehende Werte/Regeln/Historie bleiben konsistent.
- Module (z. B. FM-Firmware) können später eigene Metriken liefern, die der Agent
  samt Descriptor durchreicht (`group: module`) — nicht Teil dieses PR, aber der
  Kontrakt muss es tragen.

## 3. Server: Definitionen, Ablage, Ingest

### 3.1 `MetricDefinition` (DB, kein Katalog im Code)

Tabelle, global (nicht pro Station), eine Zeile pro Key:

- Vom Agent gelieferte Felder: `key` (unique), `label`, `unit`, `group`,
  `precision`, `min_value`, `max_value`, `source_agent_version`, `first_seen`,
  `last_seen`.
- Operator-Felder (UI): `label_override`, `min_override`, `max_override`,
  `is_hidden`, `is_blocked`.
- **Auflösung:** Override gewinnt immer. Agent-Felder: **letzter Schreiber gewinnt**
  (der Agent hat keine echte Versionsnummer — `__version__` ist seit Beginn `0.1.0`,
  Builds laufen über SRCREV). Bei gemischten Builds kann ein Label kurz wechseln;
  das ist rein kosmetisch, weil die Einheit im Key steckt. `source_agent_version`
  wird nur informativ gespeichert.
- Neuer Key → Zeile automatisch anlegen, sofort sichtbar und alarmierbar.
- `is_blocked`: Werte dieses Keys werden beim Ingest verworfen (Agent-Bug-Schutz).
  `is_hidden`: gespeichert + alarmierbar, aber nicht in der Station-Ansicht.
- **Flutschutz:** globale Obergrenze 2000 Definitionen; darüber werden neue Keys
  nicht angelegt (Werte verworfen, Warnung geloggt, einmal pro Key und Stunde).
- Plausibilitätsbereich = Override, sonst Agent-Wert. Liegt **irgendein** Aggregat
  (min/max/avg) außerhalb, wird der ganze Wert dieses Keys für diesen Heartbeat
  verworfen, geloggt und `last_rejected_at` + `last_rejected_value` an der Definition
  gesetzt — in der Metrik-Verwaltung sichtbar, damit ein zu enger Agent-Bereich nicht
  still Messwerte (und damit Alarme) schluckt. Strom darf negativ sein (INA226 misst ±).
- Werte zu Keys **ohne** Definition (Descriptor fehlt noch): gespeichert, nicht
  angezeigt, nicht alarmierbar; Antwort fordert Descriptors an.
- Zeilen werden nie automatisch gelöscht (Regeln + spätere Historie referenzieren
  den Key); Operator kann ausblenden/sperren.
- Übersetzung: Agent-Labels sind Englisch und nicht über gettext übersetzbar;
  deutsche Bezeichnung bei Bedarf per `label_override`.

### 3.2 Aktuelle Werte

Neues Feld `StationTelemetry.metrics` (JSONField) — **eine** Migration, danach nie
wieder: `{key: {"min","max","avg","ts"}}`. Wird pro Heartbeat ersetzt
(nicht gemerged — fehlende Keys verschwinden).

### 3.3 Ingest + Rückwärtskompatibilität

`ingest_metrics(station, payload)` im Heartbeat-View (`apps/api/views.py`),
fehlerisoliert wie `ingest_telemetry`.

**Alte Agents** schicken keinen `metrics`-Block. Für die Übergangszeit leitet der
Server die heutigen Metriken aus `inventory` ab (`cpu.temperature_c` →
`cpu.temp_c`, `ram.usage_percent` → `mem.used_pct`, Disks → `disk.<mount>.used_pct`,
Uptime → `sys.uptime_s`), **nur wenn** der jeweilige Key nicht im `metrics`-Block
steht. So greifen die migrierten Alarme sofort für alle Stationen.
Die `MetricDefinition`-Zeilen für genau diese Legacy-Keys legt eine
**Datenmigration** an (Daten, kein Code-Katalog) — alte Agents liefern keine
Descriptors.

## 4. Metrik-Umfang (User-Entscheidung: alles außer „optional")

| Gruppe | Keys | Quelle (Agent) |
|---|---|---|
| System | `cpu.temp_c`, `cpu.usage_pct` (Mittel übers Heartbeat-Intervall, `/proc/stat`-Delta), `cpu.freq_mhz`, `sys.load1/load5/load15`, `mem.used_pct`, `swap.used_pct` (nur wenn Swap existiert), `sys.uptime_s` | `/proc`, `/sys` |
| Disk | `disk.<mount>.used_pct` | wie heute |
| Netz | `net.server_rtt_ms` (Dauer des Heartbeat-POST, gemessen im Agent — **wird heute nicht gemessen, neu**), `net.<if>.rx_kbps`, `net.<if>.tx_kbps`, `net.<if>.errors`, `net.<if>.drops` (Delta pro Intervall) | `/proc/net/dev`, Agent |
| Strom | `pwr.12v.voltage_v`, `pwr.12v.current_a`, `pwr.12v.power_w`, dito `pwr.5v.*` — je min/max/avg | hwmon (INA226) |

Interfaces: `lo` und virtuelle (`veth*`, `docker*`, `br-*`) ausschließen.

**Agent-Sampling:** ein Sammler-Thread mit ~1 Hz für PowerBoard und CPU-Auslastung,
min/max/avg pro Heartbeat-Fenster; die übrigen Werte einmal pro Heartbeat.
PowerBoard über hwmon: Gerät über den Device-Pfad `1-0040` (5 V) / `1-0041` (12 V)
zuordnen, **nicht** über die `hwmonN`-Nummer (wechselt zwischen Boots).
hwmon-Einheiten: `in1_input` mV, `curr1_input` mA, `power1_input` µW.

## 5. Generische Schwellwert-Regeln

### 5.1 Modell

`AlertRule` wird erweitert (kein neues Modell — Alerts, Notifications,
Empfänger-Routing über Topology und UI hängen daran):

- `alert_type` bekommt einen neuen Wert `threshold`; `unique=True` auf `alert_type`
  fällt weg (Ereignis-Typen bleiben per Constraint eindeutig).
- Neue Felder (nur für `threshold` relevant): `metric_key`, `operator` (`gt`/`gte`/`lt`/`lte`),
  `aggregate` (`avg`/`min`/`max`, Default `avg`), `duration_seconds` (Default 0),
  `name`.
- Alle Felder in der UI editierbar; Regeln anlegen/löschen in der UI.
- `metric_key` = Key einer `MetricDefinition` **oder** Muster mit genau einem `*`
  als ganzem Segment (`disk.*.used_pct`), das mindestens eine Definition trifft
  (Validierung; UI = Auswahl aus den Definitionen + Muster-Vorschläge). Muster gelten
  für alle Instanzen, auch später neu auftauchende.

### 5.2 Auswertung (`check_alerts`, läuft alle 30 s)

Eine generische Funktion ersetzt `_check_cpu_temperature`, `_check_disk_usage`,
`_check_ram_usage`. Pro (Station, Regel, Instanz):

- Verletzung beginnt → Zeitpunkt merken (`breach_since`).
- Verletzung hält ≥ `duration_seconds` → Alarm anlegen (einmal, solange offen).
- Wert wieder im grünen Bereich für ≥ `duration_seconds` → Alarm auto-resolve
  (`ok_since`). Gleiche Dauer in beide Richtungen = Hysterese gegen Flattern.
- Kein aktueller Wert (Station offline / Key fehlt) → Zustand einfrieren, weder
  auslösen noch auflösen (Offline hat seinen eigenen Alarm).
- Zustand persistent (kleine Tabelle oder JSON auf `StationTelemetry`), damit ein
  Neustart von `check_alerts` die Dauer nicht verliert.
- Alarm-Text generisch aus der `MetricDefinition`: „{Label} auf {Station}: {Wert}{Einheit}
  {unter/über} {Schwelle}{Einheit} seit {Dauer}".
- **Regel geändert oder gelöscht:** offene Alarme dieser Regel werden
  auto-resolved und ihr Auswertungs-Zustand verworfen (Zustand hängt per CASCADE an
  der Regel); danach wird mit den neuen Werten frisch ausgewertet. Gilt für UI und API.
- Bei mehreren Regeln auf dieselbe Metrik (Warnung + Kritisch) dürfen beide
  gleichzeitig offen sein (wie heute bei Disk).

Hinweis Auflösung: Heartbeat-Default 60 s, `check_alerts` 30 s — Dauern kleiner
als ein Heartbeat-Intervall verhalten sich wie 0.

### 5.3 Datenmigration (Startwerte)

- `cpu_temperature` (80, warning) → threshold `cpu.temp_c gte 80`, avg, 0 s
- `disk_warning` (90) / `disk_critical` (95) → `disk.*.used_pct gte 90 / 95`
- `ram_critical` (90) → `mem.used_pct gte 90`
- `gte` statt `gt`, weil die heutige Engine mit `>=` vergleicht — gleiches Verhalten an der Schwelle.
- **Bestehende, vom Operator geänderte Werte** (Schwelle, aktiv, Schweregrad)
  werden **übernommen**, nicht die Defaults. Bestehende `Alert`-Zeilen behalten
  ihre `alert_rule`-FK.
- **Beim Umstellen offene** CPU/Disk/RAM-Alarme werden von der Migration
  auto-resolved; die neue Engine löst bei weiterhin verletzter Schwelle frisch aus
  (pro Disk-Instanz). Kurzer, einmaliger Notification-Schub ist akzeptiert —
  sauberer als alte Per-Station-Alarme in den neuen Per-Instanz-Zustand zu mappen.
- Neu: `pwr.12v.voltage_v lt 11.0`, avg, **60 s**, warning.
- Weitere neue Metriken ohne Default-Regel (Operator legt bei Bedarf an).
- `create_default_alert_rules` und `0002_seed_default_rules` entsprechend anpassen.

## 6. UI

- **Alarm-Einstellungen** (`monitoring/alert_settings.html`): Ereignis-Regeln wie
  heute; darunter Schwellwert-Regeln als Liste mit Inline-Edit aller Felder,
  „Regel hinzufügen", Löschen. DE-Locale-Regel: `lang="en"`/dot-decimal an allen
  Zahl-Inputs. `AlertRuleUpdateView` erweitern bzw. eigene Create/Delete-Views,
  gleiche Berechtigung (`area="alert_rules"`, `verb="manage"`).
- **Metrik-Verwaltung** (neu, gleiche Berechtigung `alert_rules`): Liste aller
  `MetricDefinition`s mit Agent-Label, Einheit, Gruppe, zuletzt gesehen; editierbar
  `label_override`, `min_override`/`max_override`, ausblenden, sperren;
  „zuletzt verworfen"-Hinweis bei `last_rejected_at`.
- **Station-Detail:** neuer Abschnitt/Tab „Metriken", generisch aus den `group`s
  gerendert (System / Netz / Strom / Disk): aktueller Wert, bei min/max/avg alle drei,
  Hervorhebung wenn eine Regel verletzt ist. Keine Diagramme (kommen mit der Historie).
  `frontend-design`-Skill ist Pflicht (Projektregel).
- Alter Inventory-Tab bleibt (Hardware-Infos), Messwerte dort dürfen auf den neuen
  Abschnitt verweisen.

## 7. API

- Read-Serializer der Station liefert `metrics` (aktuelle Werte) mit Label/Einheit
  aus `MetricDefinition`.
- `AlertRule` in der Automation-API: neue Felder lesen/schreiben (Phase-3-Rechte
  für AlertRule gelten unverändert), Validierung wie in der UI.
- `GET /api/v1/metrics/definitions/` (read-only für Leser; Override/hidden/blocked
  schreibbar mit `alert_rules.manage`).
- drf-spectacular-Schema muss die neuen Felder sauber zeigen.

## 8. Agent + linux-image (Begleit-PR)

- **Agent** (lebt in `station-manager/station_agent`, gleicher PR): `metrics`-Sammler
  nach §4, feature-detect + graceful degrade für jede Quelle, keine neue
  Python-Dependency.
- **linux-image** (eigener PR, nach dem station-manager-Merge): `dtparam=i2c_arm=on`,
  `CONFIG_SENSORS_INA2XX`, DT-Overlay auf `i2c1` mit `ti,ina226` @ `0x40`/`0x41`,
  `shunt-resistor = <10000>` (µΩ), nur rpi-Maschine; plus SRCREV-Bump des Agents
  per `scripts/pin-station-agent.sh`.
- Telemetrie-Kontrakt (Projekt-Memory `contract/station-telemetry-interface`) um
  Punkt 5 ergänzen: linux-image liefert hwmon für INA226, Agent liest es.

## Dem Builder überlassen

- Exakte Modul-/Dateinamen, ob der Regel-Zustand als Tabelle oder JSON liegt.
- Ob der Inventory-Fallback (§3.3) im Ingest oder in einem Adapter lebt.
- Aufteilung der Agent-Sammler in Module; Sampler-Thread-Details.
- Konkretes Layout der Metrik-Ansicht (innerhalb der Projekt-Designsprache).
- Plausible Wertebereiche und Labels je Agent-Descriptor.
- Speicherort des Descriptor-Hashs pro Station.

## Tests (Mindestumfang)

- Descriptors: Anlage, letzter Schreiber gewinnt, Override gewinnt, blocked
  verwirft, Obergrenze, Hash pro Station (unbekannt/abweichend →
  `metric_descriptors_required`), Server-Nachrechnung = Agent-Hash.
- Plausibilität: ein Aggregat außerhalb → ganzer Wert verworfen + `last_rejected_*`;
  Override-Bereich gewinnt.
- Ingest: skalare + min/max/avg-Werte, Keys ohne Definition, ungültige Keys/Werte,
  >200 Keys, fehlender Block, Inventory-Fallback für alte Agents.
- Engine: Dauer erreicht/nicht erreicht, Hysterese beim Auflösen, kein Wert =
  eingefroren, Muster-Keys (mehrere Disks), Warnung+Kritisch parallel,
  Zustand überlebt Neustart.
- Datenmigration: übernimmt geänderte Operator-Werte, bestehende Alerts behalten FK.
- UI/API: Rechte (nur `alert_rules.manage` schreibt), Validierung `metric_key`.
- Agent: jede Quelle fehlt → Key fehlt, kein Crash; hwmon-Zuordnung über Device-Pfad;
  CPU-%/Netz-Deltas; Descriptor-Hash stabil, volle Liste nur bei Start/Änderung/Anforderung.

## Nachtrag 2026-11-11: APRS-RX-Health als generische Metrik

`aprs.rx_packets_1h` (Einheit `pkts`, Gruppe `other`, 0..∞) kommt aus einer extern
registrierten Quelle (`MetricsCollector.add_source`) im `AprsDecoder`; eine Metrik pro Station
(Summe über Slots, kein Instanz-Segment). Der Key wird **weggelassen**, solange kein APRS-Decoder
läuft oder einer < 60 min läuft — "fehlender Wert friert die Regel ein" ersetzt den früheren
Sonder-Alarm `passive_no_rx`. Seed-Regel: `lt 1`, 5 h Dauer, Warnung.
