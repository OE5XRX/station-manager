# station-manager

Repo-spezifische Architekturnotizen. Übergeordnete Projektstrategie, Arbeitsprozesse und Deploymentkonventionen → `OE5XRX/CLAUDE.md` (im Meta-Ordner).

## Architektur — station-manager

### Web-Push / PWA (`apps/webpush`)
`apps/webpush` ist der dritte Alert-Kanal neben E-Mail und Telegram. Das Feld `User.notify_channel` (Enum `EMAIL/PUSH/BOTH`, Default `EMAIL`) steuert das Routing pro User. Wichtige Invarianten:

- **PUSH ohne registriertes Gerät fällt auf E-Mail zurück** — kein Alert geht verloren.
- **SW + Manifest werden als Django-Views serviert** (nicht als Static Files), weil WhiteNoise Dateinamen durch Content-Hashing umbenennt — Service Worker (`/sw.js`, mit `Service-Worker-Allowed: /`) und Manifest (`/manifest.webmanifest`) brauchen stabile, un-gehashte Root-URLs. Beide Routes liegen locale-frei (außerhalb `i18n_patterns`).
- **iOS** benötigt eine installierte PWA (ab iOS 16.4), bevor Push-Abonnements möglich sind — kein Push an Mobile-Safari ohne Homescreen-Install.
- **VAPID-Keys** werden einmalig per `manage.py generate_vapid_keys` erzeugt und als Env-Variablen / Secrets hinterlegt (nie in DB oder Repo). Ohne Keys ist `ALERT_WEBPUSH_ENABLED = False` und der Kanal wird still deaktiviert — kein Fehler.
- **Subscription-Lebenszyklus:** Abgelaufene oder gesperrte Subscriptions (HTTP 404/410 vom Push-Dienst) werden beim nächsten Send automatisch aus der DB entfernt. Fehler einer Subscription isolieren die anderen.

### Generische Metriken + Schwellwert-Alarme (`apps/monitoring`)
Spec: `docs/superpowers/specs/2026-11-03-generic-metrics-design.md`. Messwerte laufen über einen
generischen, vom Agent selbst beschriebenen `metrics`-Block im Heartbeat — **ein neuer Messwert ist
nur eine Agent-Änderung** (Key + Descriptor in `station_agent/metrics.py`), kein Server-Code, keine Migration.

- **Ein gemeinsamer Kontrakt:** `station_agent/metrics_contract.py` (Key-Regex, `mount_to_instance`,
  `normalize_value`, `clean_descriptor`, `descriptors_hash`, Muster-Matching) wird von Agent **und**
  Server importiert. Nie eine zweite Implementierung bauen — Hash/Keys würden auseinanderlaufen.
- **Keys sind stabil** (Einheit im Namen, `_v`/`_pct`); die spätere Historie baut darauf. Einheit ändern = neuer Key.
- `MetricDefinition` (global, DB): Agent-Felder „letzter Schreiber gewinnt", Operator-Overrides gewinnen immer;
  `is_blocked` verwirft Werte, Plausibilitätsbereich verwirft + setzt `last_rejected_*`. Cap 2000 Zeilen.
- Ingest (`metrics_ingest.py`) ist fehlerisoliert im Heartbeat-View; `metric_descriptors_required` nur, wenn
  der Agent die Liste in diesem Heartbeat **nicht** mitgeschickt hat (sonst Endlos-Resend).
- Schwellwert-Regeln = `AlertRule(alert_type="threshold")` mit Dauer + Hysterese; Zustand in `ThresholdState`.
  Regeländerung/-löschung resettet offene Alarme per Signal (gilt auch für Admin). Ereignis-Alarme
  (offline, OTA, Reboot, Power, Storage) bleiben typisiert.

### Telemetrie-Historie (`apps/history`)
Spec: `docs/superpowers/specs/2026-11-05-station-telemetry-history-design.md`. `MetricSample` (roh, pro
Heartbeat min/max/avg, 30 d) + `MetricHourly` (min/max/avg/count, 730 d), Composite-PK `(station, metric_key, ts)`.

- **Nur auf Postgres partitioniert** (monatlich RANGE auf `ts`, `<table>_pYYYYMM`, handgeschriebene DDL in
  `0001_initial`); SQLite = normale Tabellen. Model-Felder und DDL müssen synchron bleiben. Keine DEFAULT-Partition;
  die Migration legt aktuellen + 3 Monate an, danach `history_maintain`.
- **Betrieb:** eigener Compose-Service `history-maintainer` (`python manage.py history_maintain --loop --interval 600`),
  vorhanden in `docker-compose.yml` und `deploy/docker-compose.prod.yml`; das servers-Repo braucht ihn ebenfalls.
  Reihenfolge compact → Partitionen anlegen → droppen. Raw-Partitionen werden nur gedroppt, wenn ihr Ende
  `<= HistoryState.compacted_until` liegt (Watermark) — nie unverdichtete Stunden verlieren. Retention-Settings
  `< 1` zählen als 1 Tag; eine Partition mit Ende `> now` wird nie gedroppt.
- **Ingest** (`record_samples`, aus `ingest_metrics`) nur für akzeptierte Werte, in einem Savepoint, fehlerisoliert
  (eine Error-Log-Zeile pro Stunde). Ein Fehler darf nie den Heartbeat oder die äußere Transaktion kaputtmachen.
  Werte mit `|x| > HISTORY_MAX_ABS` (1e15) oder nicht-endlich werden nicht aufgezeichnet (sonst AVG-Overflow
  in der Postgres-Verdichtung → Watermark steht für alle Stationen).
- **API** `/api/v1/stations/{id}/metrics/history/` — Rechte exakt wie `/telemetry/`; Grenzen: 10 Keys, 730 d,
  `auto` ≤ 1000 Punkte, explizit ≤ 2016 aus der Raw-Tabelle (`raw`/`5m`, = 7 d 5m; raw geschätzt mit 6 s =
  Heartbeat-Throttle 10/min) bzw. ≤ 5000 aus der Hourly-Tabelle (`1h`/`1d`). Aggregation immer in UTC
  (Projekt-TZ ist Europe/Vienna).
- PG-Verhalten wird nur im CI-Job „History tests (Postgres)" getestet (`config.settings.test_postgres`,
  `tests/test_history_*.py`, Fixture `require_postgres`).

## station-agent

### Control-Device Single-Owner-Invariante
Ein Slot-Control-Device (`/dev/oe5xrx/slotN/control`, real CDC-ACM `ttyACM*`) ist **eine
einzige** serielle Leitung. **Jeder** Zugriff darauf — Command, Telemetry-Poll, Re-Discovery
**und** der Heartbeat-Inventory-Scan — darf immer nur **einen** gleichzeitigen Opener haben.
Zwei unkoordinierte `open()` auf dieselbe Leitung interleaven ihre Writes/Reads: die Antwort
des einen zerschießt die des anderen (z.B. ein `MODULE-RESULT` des Polls landet in der
`MODULE-LIST`-Antwort der Probe → `probe_slot` gibt `None` → Slot fällt aus dem Inventory).

Die Invariante wird auf **zwei** Ebenen durchgesetzt:

1. **Prozessweit: Path-keyed `threading.Lock` auf dem Device-Pfad** — `devlock.control_device_lock`,
   akquiriert von **JEDEM** Opener: `slot_control.SlotControl.execute` (Commands/Polls),
   `slot_discovery.probe_slot` (Discovery **und** Heartbeat-Scan). Das ist die einzige Ebene,
   die thread-übergreifend greift: der Heartbeat läuft in einem **separaten Thread** (nicht im
   Control-Event-Loop), eine `asyncio.Lock` kann ihn nicht serialisieren. Der Lock wird **vor**
   dem `open()` geholt und erst **nach** dem `close()` freigegeben — er deckt den **gesamten**
   open→configure→converse→restore→close-Zyklus. Ein `flock` auf dem bereits offenen fd würde
   das Öffnen/Konfigurieren (Baud, Raw-Mode, Input-Buffer-Reset) und das termios-Restore/Close
   ungeschützt lassen (Copilot #151 Runde 2) — beides perturbiert die Leitung. Timeout = fail
   closed (`None`/Timeout-Result), nie ein zweiter paralleler Owner. Cross-Prozess (z.B. manueller
   `selftest` bei laufendem Agent) deckt die operative HW-Disziplin (genau EIN Zugriff), nicht
   dieser In-Prozess-Lock.
2. **Intra-Loop-Optimierung: Broker-Per-Slot-`asyncio.Lock`** (`Broker._slot_locks`) — serialisiert
   Command/Poll **innerhalb** des Control-Loops (in `_execute`). `Broker.rediscover(discover_fn)`
   hält diese Locks **NICHT** (control_client ruft `discover_slots()` dennoch nie direkt, sondern
   immer über `rediscover`): Alle Slot-Locks für den ganzen sequentiellen Scan zu halten würde
   Cross-Slot-Head-of-Line-Blocking erzeugen — eine langsame/tote Probe blockierte Commands/Telemetry
   auf **jedem** gesunden Slot über das Server-Command-Timeout (10 s) hinaus (Copilot #151 Runde 3).
   Die Pro-Device-Serialisierung macht der `devlock`, nicht diese Locks. `rediscover` ist dennoch
   cancel-sicher: wird der Loop beim Disconnect mitten im Scan gecancelt, läuft die Executor-Future
   unter `asyncio.shield` aus (`await asyncio.wait`), bevor die Cancellation weitergereicht wird —
   keine verwaiste Probe überlebt den Call.

Ergänzend:
- `Broker._execute` gibt bei fehlendem Control-Pfad (`_control_path is None`) sauber
  `unknown_slot` zurück statt `os.open(None)` zu werfen.
- `_SlotInventoryDebouncer` (control_client) hält ein kurz fehlendes Modul noch N Zyklen,
  damit ein transienter Probe-Miss (inkl. flock-Timeout) das Inventory nicht flappt.

**Warum:** Regression aus PR #135 — die neu eingeführte Re-Discovery-Schleife scannte die
Leitung ohne den Broker-Lock und kollidierte mit dem Telemetry-Poll → das FM-Modul
„verabschiedete sich" im 30s-Takt auf der echten Station. Der Heartbeat-Scan war ein **dritter**
Opener, den selbst der Broker-Lock nicht erreichte — deshalb der prozessweite flock. Im Sim
unsichtbar (pty + schnelles native_sim-Timing, kein paralleler Poll im Test) — siehe
Ehrlichkeits-Regel unten.

### Boot-Marker-Commit-Gate (`station_agent/boot_markers.py`)
CM4-Boot-Stufe 1/2 (linux-image, Spec `2026-11-04-slot-boot-stage2-overlays-design.md` §5) schreiben
`oe5xrx.stage2=ok|missing|failed` und `oe5xrx.overlays=ok:<a,b>|failed:<name>` in die cmdline.
Während eines OTA-Trials (`upgrade_available=1`) läuft das als zusätzlicher Pre-Commit-Check
(`run_health_checks(boot_gate=True)`) → gleicher `rolled_back`-Pfad wie jeder Health-Check, **plus aktiver
Rollback** (Koordinator-Entscheidung 2026-11-04, Spec §7 HW 3/4): `bootloader.arm_trial_rollback` setzt
`bootcount=bootlimit` (nur 1–8, u-boot-`setexpr` rechnet hex; sonst kein Reboot) und der Agent rebootet
einmal → der **bestehende** Bootloader-Rollback-Zweig (`upgrade_available=1 && bootcount>bootlimit`, GRUB
und u-boot gleich) wechselt den Slot. Keine eigene Slot-Logik im Agent. Andere Health-Check-Fehler bleiben
passiv (PM-Inbox #34).

- **Kein Marker = alter Bootpfad = kein Blocker** (sonst rollt jede noch nicht neu geflashte Station
  jedes Update zurück). Kein procfs (Container/Sim) blockiert nicht; procfs da, aber cmdline
  unlesbar oder versteckt (`ProcSubset=pid` ⇒ ENOENT) = fail closed.
- **Vorhandener, aber unverständlicher Marker = fail closed.** Mehrfach-Vorkommen: das letzte gewinnt.
  Tokenizer = Port von Kernel `next_arg()`/`parse_args()`. Marker nach `--` (init-Args) überschreiben
  nie einen Kernel-seitigen; steht einer nur dort, zählt er trotzdem (kein Legacy/fail-open).
- Im `(0, 0)`-Retry-Fall (lokal schon committet) kein Gate. Hardware-Zustände (PowerBoard fehlt)
  sind nie ein Blocker — nur die Marker.
- Metriken `boot.stage2_ok` / `boot.overlays_failed` kommen aus demselben Parser (nur bei vorhandenem Marker;
  ein unparsebarer Overlays-Marker zählt als 1).

### Serial-Boundary Ehrlichkeits-Regel
Ein Bug am Serial-/Modul-Boundary (Modul nicht gefunden/lesbar, Control-Knopf fehlt)
gilt erst als gefixt, wenn `python -m station_agent selftest serial` auf **echtem CM4**
grün ist. Sim-grün (QEMU/native_sim) ist notwendig, nicht hinreichend — der Simulator
kann Timing-/termios-Effekte des echten UART nicht vollständig nachbilden.
