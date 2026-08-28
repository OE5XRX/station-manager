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
