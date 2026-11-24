# Benachrichtigungen in der Sprache des Empfängers — Design

Follow-up zu `2026-11-24-i18n-de-design.md` (PR #224, dort als Nicht-Ziel „Alarm-Texte … eigenes Thema:
pro Empfänger `translation.override`“ geführt). Glossar + **Du-Form** von dort gelten unverändert.

## Problem

Nachrichten, die ohne (oder nicht für den) Request erzeugt werden, sind nicht in der Sprache des
Empfängers. Seit #224 tragen die Links darin schon das Sprachpräfix des Empfängers (`reverse_for_user`),
Betreff/Body/Push-Titel aber nicht:

| Pfad | Auslöser | heute |
|------|----------|-------|
| Alarm-E-Mail (`monitoring.notifications._send_email_notification`) | `check_alerts` (Management-Command, keine aktive Sprache) | englische f-Strings, **eine** Mail an alle Empfänger |
| Alarm-Web-Push (`_send_webpush_notification`) | dito | englische f-Strings, ein Payload für alle |
| Alarm-Telegram (`_send_telegram_notification`) | dito; Empfänger ist ein Gruppen-Chat, kein User | englisch |
| Test-Mail / Test-Push / Test-Telegram (`send_test_notification`) | Admin-Klick (Request) | englisch; Rückmeldetexte an die UI unmarkiert |
| Account-Mails welcome/reset/verify (`accounts.emails.send_account_email`) | Admin-/User-Aktion (Request) | Templates ohne `{% trans %}`; würden in der Sprache des **Auslösers** gerendert |

Vollständige Suche (`send_mail`, `EmailMessage`, `mail_admins`, `render_to_string`, `send_web_push`,
`telegram`): es gibt keine weiteren Sendepfade (kein Celery; `check_alerts` ist der einzige
Hintergrund-Auslöser von Alarm-Nachrichten).

## Ziele

1. Jede Nachricht an einen User wird unter `translation.override(<Sprache des Empfängers>)` gerendert —
   Betreff, Body, Push-Titel/-Body, Severity-Label (`get_severity_display()` ist lazy und wird erst im
   override aufgelöst), Zeitstempel-Format.
2. Fan-out: **pro Sprache** rendern. Alarm-/Test-E-Mails gehen als eine Mail je Sprachgruppe (Verhalten
   „eine Mail an mehrere Empfänger“ bleibt pro Gruppe erhalten); Push wird je User gerendert.
3. Empfänger ohne User bzw. mit ungültiger/leerer Sprache → `settings.LANGUAGE_CODE`. Das gilt auch für
   Telegram (Gruppen-Chat) — explizit `override(LANGUAGE_CODE)`, nicht „was gerade aktiv ist“.
4. Request-gebundene Texte (Rückmeldung von „Test senden“ an die UI) bleiben in der Request-Sprache und
   werden **außerhalb** des Empfänger-override erzeugt.
5. Gespeicherte DB-Texte (`Alert.title`, `Alert.message`, Audit-Log) bleiben unübersetzt — bewusst.

## Design

### Sprach-Helfer (`apps/accounts/language.py`)

```python
def language_for(user) -> str:
    """User.language, wenn unterstützt, sonst settings.LANGUAGE_CODE (auch für user=None)."""

def group_by_language(items, key=lambda item: item) -> dict[str, list]:
    """Items nach language_for(key(item)) gruppieren (stabile Reihenfolge)."""
```

`reverse_for_user` nutzt `language_for` (Fallback damit deterministisch `LANGUAGE_CODE` statt „die gerade
aktive Sprache“ — im Request-losen Pfad identisch, im Request-Pfad korrekter).

### Account-Mails

`send_account_email` rendert Subject + Body unter `override(language_for(user))`. Gilt auch für
`override_to` (Verify an die neue Adresse) — die gehört demselben User. Templates
(`templates/accounts/emails/*.txt`) bekommen `{% load i18n %}` + `{% blocktrans trimmed %}`-Absätze;
der `actor`-Default „An admin“ wird übersetzbar. Zeilenumbrüche im Body bleiben erhalten (Absätze als
eigene Blöcke, `trimmed` nur wo ein Absatz einzeilig wird).

### Alarm-Benachrichtigungen

- **E-Mail:** Empfänger als `(email, language)` laden, nach Sprache gruppieren, je Gruppe Subject/Body
  unter override bauen und `send_mail` an die Gruppe. Fehler einer Gruppe werden geloggt und blockieren die
  anderen nicht. Die „keine Empfänger“-Warnung bleibt.
- **Web-Push:** Payload je User unter `override(language_for(user))`.
- **Telegram:** `override(settings.LANGUAGE_CODE)`.
- Texte über `gettext` mit benannten Platzhaltern (`%(severity)s` …); das `[OE5XRX]`-Präfix bleibt.
- Zeitstempel: `date_format(localtime(alert.created_at), "DATETIME_FORMAT")` statt `str(datetime)` —
  folgt der Empfängersprache.

### Test-Benachrichtigungen

Test-Mail an den klickenden User in dessen Sprache; Fallback-Liste (keine eigene Adresse) nach Sprache
gruppiert. Test-Push in der Sprache des klickenden Users. Die an die UI zurückgegebenen Fehlertexte werden
mit `gettext` markiert und außerhalb des override erzeugt (Request-Sprache).

## Tests

- DE-User → deutscher Betreff/Body (Mail) bzw. Titel/Body (Push); EN-User → englisch.
- Gemischter Fan-out: zwei Mails (je Sprache), jede mit den richtigen Adressen; Push je User korrekt.
- Fallback: ungültige/leere Sprache und `user=None` → `LANGUAGE_CODE`; Telegram in `LANGUAGE_CODE`.
- Aktive Sprache des Aufrufers leakt nicht (Account-Mail aus DE-Request an EN-User → englisch) und wird
  nach dem Senden wiederhergestellt.
- Test-Benachrichtigung: Rückmeldetext in der Request-Sprache, Mail in Empfängersprache.
- `scripts/i18n.sh check` grün (neue msgids übersetzt).

## Nicht-Ziele

- Gespeicherte Alarm-Titel/-Texte (`Alert.title`/`message`, vom Engine englisch erzeugt) und Audit-Texte.
- HTML-Mails, Absender-Name, Telegram pro User (es gibt nur einen Gruppen-Chat).
