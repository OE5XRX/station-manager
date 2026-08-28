# PWA + Web-Push für station-manager — Design

**Datum:** 2026-08-28
**Status:** Design (Review)
**Scope:** station-manager als installierbare PWA aufs iPhone-Homescreen, mit
Web-Push-Benachrichtigungen als dritter Alert-Kanal neben E-Mail und Telegram.

## Ziel & Motivation

Der Betreiber will station-manager als App auf dem iPhone haben (Home-Screen,
Standalone-Fenster) und darüber **Push-Benachrichtigungen** für Monitoring-Alerts
empfangen — als Alternative zu den bestehenden E-Mail-Alerts.

Web-Push ist auf iOS ein Sonderfall: es funktioniert **erst ab iOS 16.4 und nur,
wenn die Web-App vorher über „Zum Home-Bildschirm hinzufügen" installiert wurde.**
Im reinen Safari-Tab ist Push nicht möglich. Deshalb sind PWA-Installierbarkeit und
Push technisch aneinander gekoppelt — beides ist Teil dieses Designs.

## Entscheidungen (aus Brainstorming)

- **Scope:** Push + installierbar (eigenes Icon, `display: standalone`), **kein**
  Offline-Caching. Fleet-Tool braucht keine Offline-Views.
- **Kanal-Logik:** Pro-User-Präferenz — jeder wählt E-Mail / Push / beides. Push ist
  opt-in pro Gerät (Browser-Permission).
- **PUSH ohne funktionierende Subscription:** **E-Mail-Fallback** (Sicherheitsnetz
  gegen verpasste Alerts), plus UI-Warnung wenn PUSH ohne registriertes Gerät gewählt.
- **Library:** direkter `pywebpush`-Einsatz in einer eigenen dünnen App, **kein**
  `django-webpush`-Package (hinkt Django-Versionen hinterher, eigenwillige
  Templates/JS), **kein** externer Dienst (self-hosted-Ansatz).

## Architektur

Neue App **`apps/webpush`** plus drei Nahtstellen im Bestand.

### Komponenten

| Komponente | Ort | Verantwortung |
|-----------|-----|---------------|
| `PushSubscription`-Model | `apps/webpush/models.py` | Persistierte Browser-Push-Subscription pro User+Gerät |
| Subscribe/Unsubscribe-API | `apps/webpush/views.py` + `urls.py` | Registrierung/Abmeldung eines Geräts (authed, CSRF) |
| VAPID-Key-Handling | `apps/webpush/vapid.py` + Management-Command | Keys generieren/laden, Public-Key an JS durchreichen |
| `send_web_push()` | `apps/webpush/dispatch.py` | Payload an eine Subscription senden, Expiry-Handling |
| SW + Manifest Views | `apps/webpush/views.py` | `/sw.js`, `/manifest.webmanifest` mit stabilen URLs |
| Subscribe-JS | `static/webpush/push.js` | Permission anfordern, `PushManager.subscribe`, POST |
| `notify_channel` | `apps/accounts/models.py` (`User`) | Kanal-Präferenz EMAIL/PUSH/BOTH |
| Kanal-Hook | `apps/monitoring/notifications.py` | `_send_webpush_notification(alert)` als 3. Kanal |
| Präferenz-Routing | `apps/monitoring/recipients.py` | Empfänger nach `notify_channel` aufteilen |
| Notification-Settings-Page | `apps/accounts` (Template + View) | Channel-Wahl + „Push auf diesem Gerät aktivieren" |
| PWA-Head | `templates/base.html` | Manifest-Link, Apple-Meta, SW-Registrierung |

### Warum eigene App statt Erweiterung von `monitoring`

Push-Subscriptions und VAPID-Handling sind ein eigenständiges Subsystem mit klarer
Schnittstelle (`send_web_push(subscription, payload)`), unabhängig testbar vom
Alert-Engine. `monitoring` konsumiert nur die Dispatch-Funktion, analog wie es heute
`send_mail` konsumiert. Hält beide Apps fokussiert.

## Datenmodell

### `PushSubscription` (neu, `apps/webpush`)

```
user            FK(User, related_name="push_subscriptions", on_delete=CASCADE)
endpoint        URLField(unique=True)      # Push-Service-URL (Apple/Mozilla/Google)
p256dh          CharField                  # Public-Key der Subscription (base64url)
auth            CharField                  # Auth-Secret der Subscription (base64url)
label           CharField(blank=True)      # z.B. "iPhone (Safari)" — aus UA abgeleitet
created_at      DateTimeField(auto_now_add=True)
last_success_at DateTimeField(null=True)
failure_count   PositiveSmallIntegerField(default=0)
```

- `endpoint` ist der natürliche Unique-Key (ein Browser-Endpoint = eine Subscription).
  Re-Subscribe mit gleichem Endpoint → Update statt Duplikat (idempotent).
- `on_delete=CASCADE`: User gelöscht → Subscriptions weg.

### `notify_channel` (neu, am `User` in `apps/accounts`)

```python
class NotifyChannel(models.TextChoices):
    EMAIL = "email", _("Nur E-Mail")
    PUSH  = "push",  _("Nur Push")
    BOTH  = "both",  _("E-Mail und Push")

notify_channel = models.CharField(
    max_length=8, choices=NotifyChannel.choices,
    default=NotifyChannel.EMAIL,
)
```

Default `EMAIL` → **Bestandsverhalten bleibt unverändert**, bis ein User aktiv Push
aktiviert. Kein Zwangs-Opt-in.

## Empfänger-Routing (Präferenz-Filterung)

`recipients_for_station_alert(station)` liefert heute ein `User`-QuerySet
(topologie-basiert). Es wird in zwei Verbraucher-seitige Helfer aufgeteilt, **ohne**
die Topologie-Logik zu duplizieren:

- `email_recipients_for_station_alert(station)` — Basis-QuerySet
  `.filter(notify_channel__in=[EMAIL, BOTH])`
  **plus** PUSH-User **ohne** aktive Subscription (E-Mail-Fallback).
- `push_recipients_for_station_alert(station)` — Basis-QuerySet
  `.filter(notify_channel__in=[PUSH, BOTH])` **und** `push_subscriptions`-exists.

Die bestehende `recipients_for_station_alert` bleibt als Basis (topologie-only) und
wird von beiden Helfern konsumiert. So bleibt die Topologie-Contract-Logik
Single-Source.

**E-Mail-Fallback-Regel exakt:** Ein User bekommt E-Mail, wenn
`notify_channel in {EMAIL, BOTH}` **ODER** (`notify_channel == PUSH` **UND** keine
aktive Subscription). Damit fällt ein „nur Push"-User mit kaputtem/fehlendem Gerät
nicht durchs Raster.

## VAPID-Keys

- Management-Command `python manage.py generate_vapid_keys` (via `py_vapid`) gibt ein
  Keypair aus. Keys landen in Secrets/Env — **analog zu den OIDC-Keys**, nie
  hartkodiert, nie in die DB.
- Settings: `WEBPUSH_VAPID_PUBLIC_KEY`, `WEBPUSH_VAPID_PRIVATE_KEY`,
  `WEBPUSH_VAPID_ADMIN_EMAIL` (als `mailto:`-Subject für den Push-Service).
- **Feature-Flag-Semantik:** Sind die Keys nicht gesetzt, ist der Push-Kanal still
  deaktiviert (`ALERT_WEBPUSH_ENABLED` wird nur wahr, wenn Keys vorhanden) — analog
  zu `ALERT_EMAIL_ENABLED` / `ALERT_TELEGRAM_ENABLED`.
- Der **Public-Key** wird an das Subscribe-JS durchgereicht (Template-Context bzw.
  Config-Endpoint). Der **Private-Key** bleibt serverseitig für `pywebpush`.

## Service Worker & Manifest — Serving

**Problem:** WhiteNoise nutzt `CompressedManifestStaticFilesStorage`, das
Dateinamen hasht (`push.abc123.js`). Ein Service Worker braucht aber eine **stabile
URL** und muss im **Root-Scope** registrierbar sein, und das Manifest wird per
festem `<link>` referenziert.

**Lösung:**
- `/sw.js` und `/manifest.webmanifest` als dünne Django-Views (kein Static-Hashing).
  - `/sw.js`: Content-Type `application/javascript`, Header
    `Service-Worker-Allowed: /` (erlaubt Root-Scope trotz Auslieferung von tieferer
    URL, falls nötig).
  - `/manifest.webmanifest`: Content-Type `application/manifest+json`.
- App-**Icons** (192×192, 512×512, plus `maskable`) als normale Static-Files —
  die dürfen gehasht werden, das Manifest referenziert sie über `{% static %}`.
- Manifest-Inhalt: `name`, `short_name`, `start_url: /`, `display: standalone`,
  `theme_color`/`background_color` passend zum bestehenden Bootstrap-Look, Icon-Set.
- `base.html`-Head: `<link rel="manifest">`, `apple-mobile-web-app-capable`,
  `apple-mobile-web-app-status-bar-style`, `apple-touch-icon`, und ein kleines
  Inline-Snippet zur SW-Registrierung (`navigator.serviceWorker.register('/sw.js')`).

Der Service Worker selbst behandelt nur `push`- und `notificationclick`-Events
(Notification anzeigen, bei Klick Fenster fokussieren/öffnen). Kein Fetch-Caching
(kein Offline).

## Subscribe-Flow (Notification-Settings-Page)

Neue Settings-Page unter `apps/accounts` (Notification-Präferenzen):

1. **Channel-Wahl** (`notify_channel`) als Radio/Select.
2. **„Push auf diesem Gerät aktivieren"**-Button:
   - JS ruft `Notification.requestPermission()`.
   - Bei Grant: `serviceWorkerRegistration.pushManager.subscribe({ userVisibleOnly:
     true, applicationServerKey: <VAPID_PUBLIC_KEY> })`.
   - Subscription (endpoint, keys) wird per POST (CSRF-Token) an den
     `apps/webpush`-Subscribe-Endpoint geschickt und persistiert.
3. **Liste registrierter Geräte** mit Entfernen-Button (Unsubscribe → DELETE
   Endpoint + `pushManager`-Unsubscribe clientseitig).
4. **iOS-Hinweis-Banner:** „Auf iPhone/iPad: erst über Teilen → *Zum Home-Bildschirm*
   installieren, dann die App vom Home-Screen öffnen und hier Push aktivieren."
   (Erklärt den iOS-16.4-Installations-Constraint.)
5. **Warn-Banner:** wenn `notify_channel == PUSH` und keine aktive Subscription
   existiert („Du bekommst aktuell E-Mails als Fallback, bis du ein Gerät aktivierst").

## Dispatch & Error-Handling

`send_alert_notifications(alert)` (in `apps/monitoring/notifications.py`) bekommt
einen dritten Zweig:

```python
if getattr(settings, "ALERT_WEBPUSH_ENABLED", False):
    _send_webpush_notification(alert)
```

`_send_webpush_notification(alert)`:
- Ermittelt `push_recipients_for_station_alert(alert.station)`.
- Iteriert über deren `push_subscriptions`, ruft pro Subscription
  `send_web_push(subscription, payload)`.
- **Jede Subscription in isoliertem try/except** — ein totes Gerät blockt die
  anderen nicht.
- Payload: `title`, `body`, `url` (Deep-Link zur Station/Alert-Detail), `severity`.

`send_web_push(subscription, payload)` (in `apps/webpush/dispatch.py`):
- Ruft `pywebpush.webpush(...)` mit VAPID-Claims.
- **`WebPushException` mit Status 404/410 → Subscription löschen** (Endpoint
  abgelaufen/abgemeldet).
- Anderer Fehler → `failure_count += 1`, Log als Warning; ab Schwelle (z.B. 5) wird
  die Subscription beim nächsten Lauf mitgeprunt.
- Erfolg → `last_success_at = now`, `failure_count = 0`.

Synchron im bestehenden Alert-Pfad (wie E-Mail heute). Async/Celery ist bewusst
**nicht** Teil dieses Designs (YAGNI — Alert-Volumen ist niedrig).

## Sicherheit

- Subscribe/Unsubscribe-Endpoints sind **authentifiziert** (nur der eingeloggte User
  legt Subscriptions für sich selbst an) und **CSRF-geschützt**.
- VAPID-Private-Key nur in Secrets/Env, nie DB/Repo (analog OIDC-Keys unter
  `/app/oidc_keys`).
- `endpoint` unique → keine fremde Subscription überschreibbar; Server prüft, dass
  ein Unsubscribe nur eigene Subscriptions trifft.
- Push-Payload enthält keine Secrets, nur Alert-Metadaten, die der User ohnehin sehen
  darf (er ist im Topologie-Empfängerkreis).

## Testing (TDD)

1. **Recipient-Filterung** (`apps/monitoring`):
   - EMAIL-User → nur im E-Mail-Set.
   - PUSH-User **mit** Subscription → nur im Push-Set.
   - PUSH-User **ohne** Subscription → im E-Mail-Set (Fallback).
   - BOTH-User mit Subscription → in beiden Sets.
2. **Expiry-Pruning** (`apps/webpush`, `pywebpush` gemockt):
   - 410-Response → Subscription gelöscht.
   - Transienter Fehler → `failure_count` erhöht, Subscription bleibt.
   - Erfolg → `last_success_at` gesetzt, `failure_count` zurück auf 0.
3. **Kanal-Dispatch** (`apps/monitoring`):
   - BOTH triggert E-Mail **und** Push.
   - PUSH ohne Gerät → E-Mail-Fallback greift (kein stiller Verlust).
   - `ALERT_WEBPUSH_ENABLED=False` → kein Push-Versuch.
4. **SW/Manifest-Views** (`apps/webpush`):
   - `/sw.js` → 200, `application/javascript`, `Service-Worker-Allowed`-Header.
   - `/manifest.webmanifest` → 200, `application/manifest+json`, gültiges JSON mit
     `display: standalone`.
5. **Subscribe-API** (`apps/webpush`):
   - POST legt Subscription an; erneuter POST gleicher Endpoint → Update statt
     Duplikat.
   - Unauth → 403/302.
   - Unsubscribe entfernt nur eigene Subscription.

## YAGNI / bewusst nicht enthalten

- Offline-Caching / Fetch-Handler im SW.
- Async-Delivery (Celery/Queue).
- Push für andere Events als Monitoring-Alerts (kann später denselben
  `send_web_push`-Pfad nutzen).
- Rich-Notifications (Bilder/Action-Buttons) über das Nötige hinaus.
- Web-Push-Analytics/Delivery-Reports.

## Offene Punkte für die Implementierung

- Icon-Assets (192/512/maskable) müssen erstellt werden — abgeleitet vom
  bestehenden OE5XRX-Branding.
- Genaue Platzierung der Notification-Settings-Page in der bestehenden
  `accounts`-Navigation.
- DE-Locale-Check der neuen Form (Radio, kein Number-Input — kein Komma-Problem hier,
  aber der Template-Comment-Guard und die `{% comment %}`-Regel gelten).
