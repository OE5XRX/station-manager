# Datenschutz (DSGVO) für OE5XRX — Design

Stand: 2026-11-18 · Entscheidungen: Peter (Obmann, OE5BPA)

## Warum

Am Freitag, 2026-11-20, ist Vereinstreffen mit Einschulung auf die Remote-Stationen. Der
station-manager speichert viel Personenbezogenes: wer welche Station öffnet, wer die
Kontrolle übernimmt, PTT, Steuerbefehle, Änderungen, Terminal-Sessions, Logins mit
IP und GeoIP. Es gibt aber weder eine Datenschutzerklärung noch Löschfristen.
Website, Beitrittsformular (Vereinsplaner) und Statuten enthalten nichts zum Datenschutz.
Ziel ist ein sauberer, ehrlicher Stand, den man den Mitgliedern bei der Einschulung
erklären kann.

## Ziele

- **Datenschutzerklärung:** eine einzige Erklärung für den ganzen Verein auf
  oe5xrx.org. Sie gilt für Mitgliederverwaltung (Vereinsplaner), Website,
  Remote-Stationen/station-manager, SSO-Apps (InvenTree, internal-web) und
  Push-Benachrichtigungen.
- **Hinweis im station-manager:** Jeder User nimmt die aktuelle Version einmal beim
  Login zur Kenntnis. Das wird nachweisbar mit Version und Zeitpunkt gespeichert.
- **Löschfristen** werden automatisch durchgesetzt.
- **Austritt in zwei Stufen:** Ein ehemaliges Mitglied bleibt 12 Monate über sein
  Rufzeichen zuordenbar, für eine eventuelle Anfrage der Fernmeldebehörde.

## Nicht-Ziele

- **Keine Einwilligung (Opt-in) für die Betriebs-Logs.** Rechtsgrundlage ist die
  Mitgliedschaft (Art. 6 Abs. 1 lit. b) bzw. das berechtigte Interesse (lit. f):
  Nachweis gegenüber der Fernmeldebehörde, Sicherheit, Missbrauchsabwehr. Eine
  widerrufbare Einwilligung würde den Betrieb blockieren.
- **Kein Self-Service** für Auskunft, Export oder Löschung. Anfragen laufen formlos
  per Mail an info@oe5xrx.org und werden manuell vom Admin erledigt.
- **Keine Rechtsberatung.** Den Text gibt der Vorstand frei; dafür ist laut § 12 der
  Statuten der Vorstand zuständig, nicht die Generalversammlung.

## Anti-Ziele

- **Kein sofortiges Komplettlöschen beim Austritt.** Sonst ist nicht mehr nachweisbar,
  wer gesendet hat.
- **IP-Adressen nicht vor normalen Mitgliedern zeigen.** Der Audit-Tab bleibt sichtbar,
  die IP sieht nur ein Admin. Das ist bereits in Arbeit, Kind `7196a0b7`.
- **Den Hinweis beim Login nicht vor der Veröffentlichung des Texts live schalten.**

## Feste Entscheidungen

| Thema | Entscheidung |
|---|---|
| Ort der Erklärung | Variante A: eine Vereins-Erklärung auf oe5xrx.org (`OE5XRX.github.io/docs/datenschutz.md`), verlinkt aus Impressum, station-manager-Footer, Hinweis beim Login und (manuell, durch Peter) aus dem Vereinsplaner-Formular |
| Verantwortlicher | Amateurfunkclub für Remote Stationen, ZVR 1785648084, 4623 Gunskirchen, vertreten durch den Obmann |
| Datenschutz-Kontakt | **info@oe5xrx.org** |
| Kontrolle, PTT, Steuerbefehle, Stations-Änderungen, Terminal-Session-Metadaten | **12 Monate**, dann löschen (Begründung: Verfolgungsverjährung § 31 VStG, 1 Jahr) |
| Account-, Login- und SSO-Audit | **12 Monate** |
| IP, User-Agent, GeoIP (Land/Stadt) in allen Logs und Sessions | **90 Tage**, dann die Felder leeren; der Eintrag selbst bleibt |
| Sessions, OAuth-Tokens, abgelaufene Einladungs- und Reset-Tokens, widerrufene TokenSessions | regelmäßig aufräumen (abgelaufen = weg) |
| Austritt, Stufe 1 (sofort) | Account sperren; Sessions und Tokens beenden. Profil löschen: E-Mail, Name, Adresse, Telefon, Koordinaten, Locator, Avatar, Bio, QRZ-Link, Push-Abos, Zuweisungen. **Nur das Rufzeichen bleibt.** |
| Austritt, Stufe 2 (nach 12 Monaten) | Den restlichen Datensatz automatisch endgültig löschen |
| Löschsperre | Ein Admin kann die Löschung für einen konkreten Fall anhalten, z. B. bei einer laufenden Behördenanfrage |
| Audio, Terminal-Inhalte | werden nicht gespeichert; das wird in der Erklärung so gesagt |

## Weitere Entscheidungen (Peter)

- Backups: ca. 12 Monate (keep-monthly 12, kein keep-yearly).
- „Deaktivieren“ (umkehrbar) **und** „Austritt“ (endgültig) — beides anbieten.

## Priorität

**Bis Freitag:**
1. Text der Datenschutzerklärung auf oe5xrx.org, plus Link im Impressum
2. Hinweis beim Login mit Versionsnummer, plus Footer-Link im station-manager
3. Job für die Löschfristen

**Danach:**

4. Austritt in zwei Stufen, plus Löschsperre
5. Text der Purge-Meldung ohne E-Mail
6. Backup-Aufbewahrung auf ca. 12 Monate (`servers/backup/restic-backup.sh`:
   `--keep-yearly 3` streichen). Klein; kann parallel zu den Punkten für Freitag laufen.

## Lösungswege, die wir abgewogen haben

- **A) Vereins-weite Erklärung auf oe5xrx.org plus Hinweis im station-manager.**
  Gewählt: Es gibt nur einen Text für alle Verarbeitungen, und das Beitrittsformular
  kann ihn mitbenutzen.
- **B) Nur im station-manager.** Schneller, aber Vereinsplaner und Website blieben
  ungeregelt, und es bräuchte später einen zweiten Text.
- **C) Nur Text plus mündliche Erklärung.** Nicht nachweisbar, wer den Text kennt.

## Inhalt der Datenschutzerklärung

Deutsch, ehrlich, konkret und kurz gehalten. Bei der Einschulung soll man ihn
durchgehen können. Abschnitte:

1. **Verantwortlicher und Kontakt** (siehe oben).
2. **Mitgliederverwaltung:** Daten aus dem Beitrittsformular. Vereinsplaner ist
   Auftragsverarbeiter. Beitrags- und Buchhaltungsunterlagen werden 7 Jahre aufbewahrt
   (BAO).
3. **Website oe5xrx.org:** gehostet auf GitHub Pages, das Server-Logs mit IP führt.
   Kein Tracking und keine Analytics; Schriften werden selbst gehostet.
4. **Remote-Stationen / station-manager (stations.oe5xrx.org o. ä.):**
   - **Profildaten.** Die Adresse wird zur Ermittlung der Koordinaten an
     Nominatim/OpenStreetMap übermittelt.
   - **Betriebsprotokoll:** Kontrolle, PTT, Befehle, Änderungen, Terminal-Sessions —
     Zweck, Rechtsgrundlage, 12 Monate.
   - **Was NICHT gespeichert wird:** Audio, Terminal-Inhalte.
   - **Sichtbarkeit:** Das Stations-Protokoll sehen eingeloggte Mitglieder ohne IP;
     IPs sehen nur Admins.
   - **Logins und Sessions:** IP und GeoIP aus einer lokalen Datenbank, also ohne
     Übermittlung an Dritte. 90 Tage.
   - **Push-Benachrichtigungen:** Sie laufen über den Push-Dienst des jeweiligen
     Browsers und sind optional.
   - **Hosting:** Hetzner Online GmbH (Auftragsverarbeiter), Rechenzentrum Nürnberg,
     Deutschland (`nbg1`, laut `servers/terraform`).
   - **Backups:** verschlüsselt mit restic, gespeichert bei Cloudflare R2 mit
     EU-Jurisdiktion. Aufbewahrung: täglich 7, wöchentlich 4, monatlich 12 → ca. 12 Monate
     (Entscheidung Peter: `--keep-yearly 3` streichen, Repo `servers`). Gelöschte Daten
     können bis zu ca. 12 Monate im Backup weiterbestehen.
     Sie werden nur zur Wiederherstellung nach einem Ausfall verwendet.
5. **Single Sign-On:** Weitergabe von Identität und Gruppen an Vereins-Apps (InvenTree,
   internal-web).
6. **Austritt:** die zwei Stufen, wie oben.
7. **Rechte:** Auskunft, Berichtigung, Löschung, Einschränkung, Widerspruch,
   Datenübertragbarkeit; Beschwerde bei der Datenschutzbehörde (dsb.gv.at).
8. **Version und Datum:** sichtbar am Seitenende, „Version 1, gültig ab …“.

Der Entwurf wird als PR geöffnet. Peter bzw. der Vorstand geben frei, bevor gemerged wird.

## Wie es gebaut wird — Website (`OE5XRX.github.io`)

- Neue Seite `docs/datenschutz.md` im Stil von `docs/impressum.md`, mit derselben
  Front-Matter- und Navigationskonvention.
- Im Impressum einen Link auf die Datenschutzerklärung ergänzen.
- Die Kontakt-E-Mail im Impressum bleibt, wie sie ist. Sie wird nur in der
  Datenschutzerklärung durch info@oe5xrx.org ergänzt.

## Wie es gebaut wird — station-manager

### Hinweis beim Login (Kenntnisnahme)

- **Settings:** `PRIVACY_POLICY_VERSION` (int, Start 1) und `PRIVACY_POLICY_URL`
  (`https://oe5xrx.org/docs/datenschutz.html`), beide per Env überschreibbar.
  Steht die Version auf 0, ist der Hinweis aus — so lässt sich das Feature deployen,
  bevor der Text live ist.
- **User-Felder:** `privacy_ack_version` (int, default 0) und `privacy_ack_at`
  (datetime, null).
- **Middleware:** Ein eingeloggter User, dessen `privacy_ack_version` kleiner ist als
  die aktuelle Version, wird auf `/privacy/ack/` umgeleitet, mit `next`-Parameter
  (nur sichere Ziele).
  - **Ausgenommen:** Logout, die Seite selbst, Static/Media, alle maschinellen Endpunkte
    (Agent-, Heartbeat- und API-Token-Auth, WebSockets, OIDC-Token-, UserInfo- und
    JWKS-Endpunkte).
  - **Nicht ausgenommen:** der OIDC-Authorize-Flow im Browser. Wer sich per SSO in
    InvenTree einloggt, sieht den Hinweis also auch.
- **Seite:** Sie fasst kurz zusammen, was gespeichert wird und wie lange, verlinkt
  die volle Erklärung und hat einen Button „Zur Kenntnis genommen“ (POST). Danach
  geht es weiter zu `next`. Die Kenntnisnahme kommt als Eintrag ins
  `AccountAuditLog`.
- **Footer:** ein Link „Datenschutz“ auf allen Seiten.
- **Verhalten:** Bis zur Kenntnisnahme ist der User von nichts ausgesperrt außer der UI.

### Job für die Löschfristen

- **Neues Management-Command** (Name nach Wahl des Builders, z. B.
  `enforce_retention`). Es ist idempotent und hat ein Flag `--dry-run`, das nur zählt.
  Die Fristen kommen aus Settings: `RETENTION_AUDIT_DAYS=365`, `RETENTION_IP_DAYS=90`.
- **Was der Job tut:**
  - Station-, Account- und SSO-Audit-Logs älter als 365 Tage löschen.
  - Abgeschlossene Terminal-Sessions älter als 365 Tage löschen.
  - In allen Audit-Logs IPs älter als 90 Tage auf NULL setzen. In `TokenSession`
    zusätzlich User-Agent, Land und Stadt leeren.
  - Die vorhandene Logik von `prune_token_sessions` aufrufen bzw. wiederverwenden,
    ebenso Djangos `clearsessions` und `cleartokens` von django-oauth-toolkit.
  - Abgelaufene `AccountToken` löschen.
  - Am Ende pro Kategorie die Zahl der betroffenen Einträge ins Log schreiben.
- **Zeitplan:** einmal täglich, über den bestehenden Mechanismus im
  `deploy/docker-compose.prod.yml` (Muster: `check_station_status --loop`). Ob das ein
  eigener Service mit Loop wird oder ein Hook in `run_background_jobs`, entscheidet der
  Builder; wichtig ist nur, dass es in Produktion wirklich läuft.
- **Löschsperre:** In Stufe 1 noch nicht nötig. Der Job muss aber so gebaut sein,
  dass ein Ausschluss pro User später leicht nachrüstbar ist. Die Sperre gilt dann für
  **beide** Fristen, also auch für das Leeren der IP nach 90 Tagen. *(Meine Entscheidung:
  Gerade die IP braucht eine Behörde oft.)*
- **Nur personenbezogene Stations-Einträge löschen.** *(Meine Entscheidung.)* Im
  `StationAuditLog` werden nach 365 Tagen nur Einträge gelöscht, die eine Person
  betreffen: `user` ist gesetzt, oder es ist eine User-Aktion (Kontrolle, PTT,
  Befehl, Zuweisung, Terminal). Reine System-Einträge (Status, OTA, Provisioning,
  Firmware) ohne `user` bleiben als Betriebsgeschichte der Station erhalten. Das
  Gleiche gilt sinngemäß für die anderen Audit-Tabellen: Einträge, die eine Person
  betreffen, werden nach 365 Tagen gelöscht.
- **Personenbezogenes aus Freitext-Meldungen entfernen.** *(Meine Entscheidung.)*
  `AccountAuditLog.message` (und andere Audit-Meldungen) schreiben derzeit E-Mail
  und IP in den Text (z. B. `apps/accounts/views.py` ~581/603/771/1005/1105/1202).
  1. **Ab sofort:** keine E-Mail und keine IP mehr in `message`. Nur Rufzeichen bzw.
     User-Referenz; die IP gehört ausschließlich ins Feld `ip_address`.
  2. **Bestehende Einträge:** eine einmalige Daten-Migration bzw. ein Job-Schritt
     entfernt E-Mails und IPs per Regex aus den alten Meldungen.
  
  Sonst würde die Erklärung nicht stimmen.
- **Weitere IP-Felder:** `AccountToken.ip_created` fällt ebenfalls unter die
  90-Tage-Regel.
- **Erster Produktionslauf:** Das Löschen ist per Setting
  `RETENTION_ENFORCE=false` ausgeschaltet; in diesem Zustand macht der tägliche Lauf
  nur einen Dry-Run und loggt Zahlen. Peter sieht sich den ersten Dry-Run an und
  schaltet das Löschen erst danach ein, nicht vor der Freigabe des Texts durch den
  Vorstand.

### Austritt in zwei Stufen (nach Freitag)

- Den jetzigen Papierkorb (Soft-Delete und manueller Purge,
  `apps/accounts/views.py:1139–1225`) zu diesem Ablauf umbauen:
  - **„Austritt“ setzen:** Stufe 1 sofort ausführen.
  - **Nach 365 Tagen:** Stufe 2 löscht automatisch, im Löschfristen-Job.
- Die Löschsperre ist ein Flag am User, mit Grund und Admin. Der Job überspringt
  solche User und ihre Audit-Einträge.
- **Deaktivieren und Austritt sind zwei getrennte Aktionen.** *(Meine Entscheidung,
  noch von Peter zu bestätigen.)*
  - **„Deaktivieren“** ist umkehrbar und entspricht dem jetzigen Soft-Delete ohne
    Datenlöschung, etwa für eine Pause.
  - **„Austritt“** ist Stufe 1 und nicht umkehrbar.
  - **Bestehende soft-gelöschte User:** Bei der Umstellung bekommen sie Stufe 1. Wer
    schon länger als 365 Tage gelöscht ist, wird sofort endgültig gelöscht.
- **Rufzeichen während der 12 Monate:** *(Meine Entscheidung.)* Ein ausgetretenes
  Rufzeichen kann nicht als neuer Account angelegt werden. Tritt die Person wieder
  ein, reaktiviert ein Admin den alten Account, und das Profil wird neu befüllt.
  So bleibt das Rufzeichen eindeutig einer Person zugeordnet.
- **Von anderen erstellte Inhalte:** `StationLogEntry`, `StationPhoto` und
  `Alert.acknowledged_by` bleiben als Vereinsinhalte erhalten. In Stufe 2 wird nur
  die Autor-Referenz entfernt (FK auf NULL). Die Erklärung erwähnt das.
- Die Purge- und Audit-Meldung enthält keine E-Mail mehr, nur das Rufzeichen.

## Offen für den Builder

- Konkrete Namen für Command und Middleware, wo genau das Command liegt, und die
  Liste der Ausnahme-URLs anhand des echten `urls.py`.
- Batch-Größen beim Löschen.
- Das Design der Hinweis-Seite. Pixel-Agent mit dem Skill `frontend-design`, im
  bestehenden Stil.

## Offen für Peter

- Den Link im Vereinsplaner-Formular setzt Peter manuell.
