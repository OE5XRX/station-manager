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

## station-agent

### Control-Device Single-Owner-Invariante
Ein Slot-Control-Device (`/dev/oe5xrx/slotN/control`, real CDC-ACM `ttyACM*`) ist **eine
einzige** serielle Leitung. **Jeder** Zugriff darauf — Command, Telemetry-Poll **und**
Re-Discovery — MUSS über den Broker und dessen Per-Slot-Lock (`Broker._slot_locks`) laufen.
Zwei unkoordinierte `open()` auf dieselbe Leitung interleaven ihre Writes/Reads: die Antwort
des einen zerschießt die des anderen (z.B. ein `MODULE-RESULT` des Polls landet in der
`MODULE-LIST`-Antwort der Probe → `probe_slot` gibt `None` → Slot fällt aus dem Inventory).

- Discovery läuft deshalb über `Broker.rediscover(discover_fn)` (hält alle Slot-Locks), **nie**
  direkt per `discover_slots()` aus dem control_client.
- `Broker._execute` gibt bei fehlendem Control-Pfad (`_control_path is None`) sauber
  `unknown_slot` zurück statt `os.open(None)` zu werfen.
- `_SlotInventoryDebouncer` (control_client) hält ein kurz fehlendes Modul noch N Zyklen,
  damit ein transienter Probe-Miss das Inventory nicht flappt.

**Warum:** Regression aus PR #135 — die neu eingeführte Re-Discovery-Schleife scannte die
Leitung ohne den Broker-Lock und kollidierte mit dem Telemetry-Poll → das FM-Modul
„verabschiedete sich" im 30s-Takt auf der echten Station. Im Sim unsichtbar (pty + schnelles
native_sim-Timing, kein paralleler Poll im Test) — siehe Ehrlichkeits-Regel unten.

### Serial-Boundary Ehrlichkeits-Regel
Ein Bug am Serial-/Modul-Boundary (Modul nicht gefunden/lesbar, Control-Knopf fehlt)
gilt erst als gefixt, wenn `python -m station_agent selftest serial` auf **echtem CM4**
grün ist. Sim-grün (QEMU/native_sim) ist notwendig, nicht hinreichend — der Simulator
kann Timing-/termios-Effekte des echten UART nicht vollständig nachbilden.
