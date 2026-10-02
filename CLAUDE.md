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
einzige** serielle Leitung. **Jeder** Zugriff darauf — Command, Telemetry-Poll, Re-Discovery
**und** der Heartbeat-Inventory-Scan — darf immer nur **einen** gleichzeitigen Opener haben.
Zwei unkoordinierte `open()` auf dieselbe Leitung interleaven ihre Writes/Reads: die Antwort
des einen zerschießt die des anderen (z.B. ein `MODULE-RESULT` des Polls landet in der
`MODULE-LIST`-Antwort der Probe → `probe_slot` gibt `None` → Slot fällt aus dem Inventory).

Die Invariante wird auf **zwei** Ebenen durchgesetzt:

1. **Prozessweit: OS-Advisory-Lock (`fcntl.flock`) auf dem Device-Node** — `devlock.control_device_lock`,
   akquiriert von **JEDEM** Opener: `slot_control.SlotControl.execute` (Commands/Polls),
   `slot_discovery.probe_slot` (Discovery **und** Heartbeat-Scan). Das ist die einzige Ebene,
   die thread-übergreifend greift: der Heartbeat läuft in einem **separaten Thread** (nicht im
   Control-Event-Loop), eine `asyncio.Lock` kann ihn nicht serialisieren. flock hängt an der
   Open-File-Description → serialisiert auch zwei Opens aus demselben Prozess. Timeout = fail
   closed (`None`/Timeout-Result), nie ein zweiter paralleler Owner.
2. **Intra-Loop-Optimierung: Broker-Per-Slot-`asyncio.Lock`** (`Broker._slot_locks`) — serialisiert
   Command/Poll/Re-Discovery **innerhalb** des Control-Loops ohne flock-Contention.
   `Broker.rediscover(discover_fn)` hält alle Slot-Locks für den Scan (control_client ruft **nie**
   `discover_slots()` direkt). `rediscover` ist cancel-sicher: wird der Loop beim Disconnect
   mitten im Scan gecancelt, läuft die Executor-Future unter `asyncio.shield` **vor** dem
   Lock-Release aus (`await asyncio.wait`), damit kein wartender Poll auf die verwaiste Probe trifft.

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

### Serial-Boundary Ehrlichkeits-Regel
Ein Bug am Serial-/Modul-Boundary (Modul nicht gefunden/lesbar, Control-Knopf fehlt)
gilt erst als gefixt, wenn `python -m station_agent selftest serial` auf **echtem CM4**
grün ist. Sim-grün (QEMU/native_sim) ist notwendig, nicht hinreichend — der Simulator
kann Timing-/termios-Effekte des echten UART nicht vollständig nachbilden.
