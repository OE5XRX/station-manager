# Benachrichtigungen in Empfängersprache — Plan

Spec: `docs/superpowers/specs/2026-11-24-i18n-notifications-design.md`. Ausführung: subagent-driven, TDD.
Tests: `DJANGO_SECRET_KEY=x .venv/bin/python -m pytest -q <files>`. Tests setzen die Sprache über
`user.language = "de"` (+ `save`). Nach jedem Task: betroffene Testdateien + `tests/test_i18n_*.py` grün,
`ruff check` + `ruff format --check` sauber.

## Task 1 — Sprach-Helfer (`apps/accounts/language.py`)

1. Test (`tests/test_i18n_notification_language.py`): `language_for(user_de) == "de"`,
   `language_for(None) == LANGUAGE_CODE`, unbekannte/leere Sprache → `LANGUAGE_CODE`; `group_by_language`
   gruppiert Users bzw. `(email, language)`-Tupel via `key`, Reihenfolge stabil; `reverse_for_user` mit
   ungültiger Sprache unter aktivem `de` → `/en/…` (LANGUAGE_CODE), nicht `/de/…`.
2. Implementieren: `language_for(user)`, `group_by_language(items, key=None)` (key Default = Identität;
   key liefert ein User-ähnliches Objekt **oder** einen Sprachcode-String), `reverse_for_user` nutzt
   `language_for`.

## Task 2 — Account-Mails (`apps/accounts/emails.py`, `templates/accounts/emails/*.txt`)

1. Tests (`tests/test_account_emails_i18n.py`): welcome/reset/verify an DE-User → deutscher Betreff +
   Body (Marker-Strings), an EN-User englisch; Aufruf unter aktivem `override("de")` an EN-User → englisch
   und danach ist `get_language() == "de"` wieder aktiv; welcome ohne `actor` → „Ein Admin“ bzw. „An admin“.
2. `send_account_email`: Rendering unter `override(language_for(user))`.
3. Templates: `{% load i18n %}`, Absätze in `{% blocktrans %}` (Variablen nur einfache Namen →
   `{% blocktrans with name=user.first_name|default:user.username %}`), Subject einzeilig.
   Body-Zeilenumbrüche erhalten. Bestehende `tests/test_account_emails.py` / `test_email_verify_flow.py`
   bleiben grün.

## Task 3 — Alarm- und Test-Benachrichtigungen (`apps/monitoring/notifications.py`)

1. Tests (`tests/test_notification_i18n.py`): Alarm an DE- + EN-Empfänger → zwei Mails, je Sprache
   richtiger Betreff/Body + richtige `to`-Liste; Push-Payload je User in seiner Sprache; Telegram unter
   aktivem `de` → `LANGUAGE_CODE`-Text (Bot gemockt); Test-Mail an DE-Admin deutsch, Rückmeldung
   (Fehlerfall) unter `override("en")` englisch / `override("de")` deutsch; Fehler beim Senden einer
   Sprachgruppe blockiert die andere nicht.
2. E-Mail: `values_list("email", "language")` → `group_by_language(key=lambda r: r[1])` → je Gruppe
   override + `send_mail` (try/except je Gruppe). Zeitstempel `date_format(localtime(...),
   "DATETIME_FORMAT")`. Strings via `gettext` mit benannten Platzhaltern.
3. Push: Payload je User unter override. Telegram: `override(settings.LANGUAGE_CODE)`.
4. `_test_email`/`_test_webpush`/`_test_telegram`: Empfängertexte unter override; UI-Rückmeldungen
   `gettext`, außerhalb des override.

## Task 4 — Katalog + Doku

`scripts/i18n.sh make`, neue msgids übersetzen (Du-Form, Glossar: Alert → Alarm), `scripts/i18n.sh check`
grün. CLAUDE.md-Abschnitt „Lokalisierung / i18n“ um eine Zeile zu Nachrichten ergänzen. Volle Testsuite.
