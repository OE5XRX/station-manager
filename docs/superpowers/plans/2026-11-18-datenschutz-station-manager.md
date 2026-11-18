# Datenschutz station-manager („Bis Freitag“) Implementation Plan

> **Peter-Entscheidungen 2026-11-18 (gelten vor dem restlichen Plan):**
> 1. Alle Builder-Entscheidungen (Version-Default 0, Footer-Link versteckt bei 0, Account/SSO-Audit komplett nach 365 d löschen, Purge-Message jetzt ohne E-Mail, Ack-URL `/<lang>/accounts/privacy/ack/`) sind bestätigt.
> 2. **Stations-IP-Wechsel-Meldung** (`apps/api/views.py:114`, „IP address changed: old → new"): fällt AUCH unter die 90-Tage-Regel — Job scrubbt IPs in `StationAuditLog.message` (und `changes`-JSON, falls dort IPs stehen) bei Einträgen > 90 d, unabhängig davon ob `user` gesetzt ist. Test ergänzen.
> 3. **Task 9 (servers-Companion-PR) wird NICHT von diesem Kind umgesetzt**, sondern vom separaten servers-Kind. Hier nur die benötigten Env-Variablen + Service-Command im PR-Body exakt auflisten.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Die station-manager-Teile der Datenschutz-Spec für Freitag: (A) einmalige, versionierte Kenntnisnahme der Datenschutzerklärung + Footer-Link, (B) täglicher Löschfristen-Job (default Dry-Run), (C) keine E-Mail/IP mehr in Audit-Freitext + einmaliges Scrubben der Altbestände.

**Architecture:** (A) zwei User-Felder + `PrivacyAckMiddleware` in `config/middleware.py` (nach `LoginRequiredMiddleware`), View/Template in `apps/accounts`. (B)+(C) neues Modul `apps/audit/retention.py` (Schritte als kleine Funktionen, einheitlicher Dry-Run/Batch-Helper, Legal-Hold-Hook `held_user_ids()`), reine Scrub-Funktion `apps/audit/scrub.py`, Command `enforce_retention` mit `--loop`, eigener Compose-Service `retention`.

**Tech Stack:** Django 6.0, django-oauth-toolkit 3.4.1, pytest (`config.settings.test`, SQLite), ruff.

**Spec:** `/home/pbuchegger/OE5XRX/specs/2026-11-18-datenschutz-design.md` (Abschnitte „Wie es gebaut wird — station-manager“ → „Hinweis beim Login“, „Job für die Löschfristen“).

**Basis:** `origin/main` @ `4dad823`. Zeilennummern unten beziehen sich auf diesen Stand. Ein Feature-Branch `feat/datenschutz` in eigenem Worktree, **ein** PR, Squash-Merge.

## Global Constraints

- Settings, alle per Env überschreibbar, Muster `int(os.environ.get(...))` wie `HISTORY_RAW_RETENTION_DAYS` in `config/settings/base.py:313`:
  - `PRIVACY_POLICY_VERSION` (int) — **Default `0` = Hinweis aus** (siehe Ambiguität 1); Produktion setzt `1`, sobald der Text auf oe5xrx.org live ist.
  - `PRIVACY_POLICY_URL` = `https://oe5xrx.org/docs/datenschutz.html`
  - `RETENTION_AUDIT_DAYS` = `365`, `RETENTION_IP_DAYS` = `90`
  - `RETENTION_ENFORCE` = `false` (Parsing wie `ALERT_EMAIL_ENABLED`: `.lower() == "true"`) → ohne `true` zählt der Job nur.
  - `RETENTION_BATCH_SIZE` = `1000`
- User-Felder: `privacy_ack_version` (PositiveIntegerField, default 0), `privacy_ack_at` (DateTimeField, null).
- Button-Text der Hinweis-Seite: „Zur Kenntnis genommen“. Footer-Linktext: „Datenschutz“.
- Bis zur Kenntnisnahme wird **nur die UI** gesperrt — nie API, Agent, WebSocket, OIDC-Token/UserInfo/JWKS. Der Browser-Flow `/sso/authorize/` wird **nicht** ausgenommen.
- Audit-`message` enthält ab sofort **keine E-Mail, keine IP, keinen User-Agent, keine GeoIP-Stadt**; IP nur im Feld `ip_address`.
- Löschen nach 365 d nur personenbezogene Einträge; reine System-Einträge im `StationAuditLog` bleiben.
- Django-Templates: **kein mehrzeiliges `{# … #}`**, nur `{% comment %}…{% endcomment %}` (CI: `scripts/check_template_comments.py`). Farben nur über Tokens (`scripts/check_color_tokens.py`).
- Keine `<input type="number">` nötig; falls doch: `lang="en"` (DE-Locale).
- CI-Gates lokal: `ruff format --check . && ruff check . && python scripts/check_template_comments.py && python scripts/check_color_tokens.py && python manage.py makemigrations --check --dry-run && pytest -q`.
- **Ausgenommen:** IP im Stations-Audit-Tab (läuft in Kind `7196a0b7`) — hier nicht anfassen.

## Review Focus

1. **HTMX-/fetch-Request eines noch nicht bestätigten Users** (z. B. Status-Polling-Partials) — erwartet: kein 302-HTML in ein Fragment, sondern `HX-Redirect` auf die Hinweis-Seite. Test in Task 2.
2. **OIDC-Login in InvenTree mit Querystring** (`/sso/authorize/?client_id=…&state=…`) — erwartet: Hinweis, danach exakt zurück zur vollen Authorize-URL inkl. Query. Tests in Task 2 + 3.
3. **Bösartiges `next`** (`//evil.example`, `https://evil.example/`, `javascript:…`) — erwartet: Fallback aufs Dashboard. Test in Task 3.
4. **Audit-Zeilen eines bereits gepurgten Users** (FK durch `SET_NULL` leer, aber Personen-Event wie PTT) — erwartet: werden trotzdem nach 365 d gelöscht; System-Zeile ohne User (Status/OTA) bleibt. Test in Task 6.
5. **Scrub-Fehlalarme**: ISO-Zeitstempel `12:30:45`, Versionsstrings `v1.2.3.4`, `1.2.3` — erwartet: unverändert; echte IPv4/IPv6 (`::1`, `2001:db8::1`) und E-Mails werden ersetzt. Test in Task 5.

---

## File Structure

| Datei | Verantwortung |
|---|---|
| `config/settings/base.py` | neue Settings, Middleware-Eintrag, Context-Processor |
| `config/settings/test.py` | `PRIVACY_POLICY_VERSION = 0` explizit (bestehende Suite bleibt grün) |
| `apps/accounts/models.py` | User-Felder; `AccountAuditLog.EventType.PRIVACY_ACKNOWLEDGED` |
| `apps/accounts/migrations/0025_privacy_ack.py` | Felder + AlterField event_type |
| `config/middleware.py` | `PrivacyAckMiddleware` |
| `apps/accounts/views_privacy.py` | `PrivacyAckView` |
| `apps/accounts/templates/accounts/privacy_ack.html` | Hinweis-Seite |
| `templates/includes/privacy_footer.html` | Footer-Link |
| `apps/accounts/context_processors.py` | `privacy(request)` |
| `apps/accounts/views.py`, `apps/sso/permissions.py`, `apps/sso/views.py` | PII aus Audit-Meldungen raus |
| `apps/audit/scrub.py` | `scrub_message()` (rein, ohne DB) |
| `apps/audit/retention.py` | Retention-Schritte, Dry-Run, Legal-Hold-Hook |
| `apps/audit/management/commands/enforce_retention.py` | Command + Loop |
| `apps/sso/models.py`, `apps/sso/management/commands/prune_token_sessions.py` | `TokenSessionQuerySet.stale()` extrahieren |
| `deploy/docker-compose.prod.yml`, `docker-compose.yml` | Service `retention` |
| Tests | `tests/test_privacy_ack_middleware.py`, `tests/test_privacy_ack_view.py`, `tests/test_privacy_footer.py`, `tests/test_audit_message_pii.py`, `tests/test_audit_scrub.py`, `tests/test_retention.py`, `tests/test_enforce_retention_command.py` |

---

### Task 1: Settings, User-Felder, Event-Type, Migration

**Files:**
- Modify: `config/settings/base.py` (neuer Block nach `HISTORY_HOURLY_RETENTION_DAYS`, ~Z. 314), `config/settings/test.py`
- Modify: `apps/accounts/models.py` (User ~Z. 175–200; `AccountAuditLog.EventType` ~Z. 269–312)
- Create: `apps/accounts/migrations/0025_privacy_ack.py` (via `makemigrations accounts -n privacy_ack`)
- Test: `tests/test_privacy_ack_view.py` (Modell-Teil)

**Interfaces:**
- Produces: `settings.PRIVACY_POLICY_VERSION: int`, `settings.PRIVACY_POLICY_URL: str`, `settings.RETENTION_AUDIT_DAYS/RETENTION_IP_DAYS/RETENTION_BATCH_SIZE: int`, `settings.RETENTION_ENFORCE: bool`; `User.privacy_ack_version: int`, `User.privacy_ack_at: datetime | None`; `AccountAuditLog.EventType.PRIVACY_ACKNOWLEDGED = "privacy_acknowledged"` (Label „Privacy Policy Acknowledged“), angehängt unter neuem Kommentar `# === Added in Datenschutz (2026-11-18) ===` (nicht umsortieren).

- [ ] **Step 1: Failing tests**
```python
def test_new_user_has_not_acknowledged(member_user):
    assert member_user.privacy_ack_version == 0 and member_user.privacy_ack_at is None

def test_privacy_ack_event_type_exists():
    assert AccountAuditLog.EventType.PRIVACY_ACKNOWLEDGED == "privacy_acknowledged"

def test_retention_defaults(settings):
    assert settings.RETENTION_AUDIT_DAYS == 365 and settings.RETENTION_IP_DAYS == 90
    assert settings.RETENTION_ENFORCE is False
    assert settings.PRIVACY_POLICY_URL == "https://oe5xrx.org/docs/datenschutz.html"
```
- [ ] **Step 2:** `pytest tests/test_privacy_ack_view.py -q` → FAIL (AttributeError).
- [ ] **Step 3:** Settings + Felder + Event-Type anlegen, `python manage.py makemigrations accounts -n privacy_ack`; in `test.py` `PRIVACY_POLICY_VERSION = 0` setzen.
- [ ] **Step 4:** Tests PASS; `python manage.py makemigrations --check --dry-run` → „No changes detected“.
- [ ] **Step 5:** Commit `feat(accounts): privacy ack fields + retention settings`.

---

### Task 2: `PrivacyAckMiddleware`

**Files:**
- Modify: `config/middleware.py` (neue Klasse unter `LoginRequiredMiddleware`), `config/settings/base.py:78` (Eintrag direkt nach `config.middleware.LoginRequiredMiddleware`)
- Test: `tests/test_privacy_ack_middleware.py`

**Interfaces:**
- Consumes: Task 1 Settings/Felder; URL-Name `accounts:privacy_ack` (Task 3 — in diesem Task als Route schon anlegen: `path("privacy/ack/", PrivacyAckView.as_view(), name="privacy_ack")` in `apps/accounts/urls.py`, View-Stub, der 200 liefert; Task 3 füllt ihn).
- Produces: `class PrivacyAckMiddleware` mit `EXEMPT_PATH_PREFIXES: Final[tuple[str, ...]]`, `EXEMPT_VIEW_NAMES: Final[frozenset[str]]`, `process_view(request, view_func, view_args, view_kwargs) -> HttpResponse | None`, `@classmethod _is_exempt_path(path: str) -> bool` (gleiche `posixpath.normpath`-Normalisierung wie `LoginRequiredMiddleware._is_public_path`, ggf. gemeinsamen Helper extrahieren).

Entscheidungen (aus dem echten `config/urls.py`):
- `process_view` statt `__call__`, damit `request.resolver_match` verfügbar ist (Locale-Präfix `/en/`/`/de/` egal). Unaufgelöste Pfade (404) laufen nie hinein.
- Gate greift nur wenn: `PRIVACY_POLICY_VERSION > 0` **und** `request.user.is_authenticated` **und** `user.privacy_ack_version < PRIVACY_POLICY_VERSION`. Token-/DeviceKey-Auth (DRF) setzt `request.user` erst in der View → ist hier anonym → automatisch frei. WebSockets laufen nicht durch HTTP-Middleware (Channels) → automatisch frei.
- `EXEMPT_PATH_PREFIXES` = `("/api/", "/static/", "/media/", "/i18n/", "/webpush/", "/sw.js", "/manifest.webmanifest", "/__debug__/")` plus **alles unter `/sso/` außer `/sso/authorize/`** (Token, Revoke, Introspect, UserInfo, Logout, `.well-known`/JWKS). `/api/` komplett, weil auch Session-Auth-JSON-Fetches dort keinen HTML-Redirect vertragen.
- `EXEMPT_VIEW_NAMES` = `{"accounts:logout", "accounts:privacy_ack", "set_language"}`.
- Redirect-Ziel: `reverse("accounts:privacy_ack") + "?" + urlencode({"next": request.get_full_path()})`; `next` nur bei `GET`/`HEAD`, sonst ohne `next`.
- HTMX (`request.headers.get("HX-Request") == "true"`): `django_htmx.http.HttpResponseClientRedirect(url)` statt 302.

- [ ] **Step 1: Failing tests** (alle mit `settings.PRIVACY_POLICY_VERSION = 1`, `client.force_login(member_user)`)
```python
def test_middleware_registered_after_login_required():  # index(PrivacyAck) == index(LoginRequired) + 1
def test_unacked_user_redirected_with_next(client, member_user, settings):
    r = client.get(reverse("dashboard:index"))
    assert r.status_code == 302 and reverse("accounts:privacy_ack") in r.url
    assert "next=" + quote(reverse("dashboard:index"), safe="") in r.url  # o.ä. via parse_qs
def test_acked_user_passes(...)                # privacy_ack_version=1 → 200
def test_old_version_ack_redirected(...)       # privacy_ack_version=1, VERSION=2 → 302
def test_version_zero_disables_gate(...)       # VERSION=0 → 200
def test_anonymous_untouched(client)           # dashboard → Login-Redirect, nicht privacy_ack
@pytest.mark.parametrize("path", ["/api/v1/health/", "/sso/userinfo/", "/sso/token/",
    "/sso/.well-known/openid-configuration/", "/sw.js", "/manifest.webmanifest", "/i18n/setlang/"])
def test_machine_paths_exempt(client, member_user, settings, path):
    assert "privacy" not in (client.get(path).get("Location") or "")
def test_logout_exempt(...)                    # POST accounts:logout → nicht auf privacy_ack
def test_ack_page_itself_not_looped(...)       # GET accounts:privacy_ack → 200
def test_sso_authorize_is_gated_and_keeps_query(...):
    r = client.get("/sso/authorize/?client_id=abc&state=xyz")
    assert reverse("accounts:privacy_ack") in r.url
    assert parse_qs(urlsplit(r.url).query)["next"] == ["/sso/authorize/?client_id=abc&state=xyz"]
def test_htmx_request_gets_hx_redirect(...):
    r = client.get(reverse("dashboard:index"), HTTP_HX_REQUEST="true")
    assert r.status_code == 200 and reverse("accounts:privacy_ack") in r["HX-Redirect"]
def test_traversal_does_not_bypass(...)        # "/api/../en/" bzw. "/sso/token/../authorize/" → gated
def test_post_redirect_has_no_next(...)
```
- [ ] **Step 2:** `pytest tests/test_privacy_ack_middleware.py -q` → FAIL.
- [ ] **Step 3:** Middleware + Settings-Eintrag + URL/View-Stub implementieren.
- [ ] **Step 4:** Datei grün; zusätzlich `pytest tests/test_login_required_middleware.py -q` grün.
- [ ] **Step 5:** Commit `feat(privacy): ack gate middleware`.

---

### Task 3: Hinweis-Seite (View + Template)

**UI-Arbeit: Der Implementer MUSS zuerst `Skill("frontend-design")` aufrufen** und die Seite im bestehenden Stil bauen (Auth-Shell-Look wie `apps/accounts/templates/accounts/login.html` / `set_password.html`: `extends "base.html"`, `{% block body %}` überschreiben, `auth-shell`/`auth-panel`, Tokens aus `static/css/tokens.css`, kein Inline-Farbwert).

**Files:**
- Create: `apps/accounts/views_privacy.py`, `apps/accounts/templates/accounts/privacy_ack.html`
- Modify: `apps/accounts/urls.py` (Stub aus Task 2 ersetzen)
- Test: `tests/test_privacy_ack_view.py`

**Interfaces:**
- Consumes: Task 1/2.
- Produces: `class PrivacyAckView(View)` mit `get(request) -> HttpResponse`, `post(request) -> HttpResponseRedirect`; Helper `_safe_next(request) -> str` (nutzt `url_has_allowed_host_and_scheme(next, allowed_hosts={request.get_host()}, require_https=request.is_secure())`, Fallback `resolve_url(settings.LOGIN_REDIRECT_URL)`).

Inhalt der Seite (Deutsch, `{% trans %}`): kurze Zusammenfassung aus der Spec — Betriebsprotokoll (Kontrolle, PTT, Befehle, Änderungen, Terminal-Session-Metadaten) 12 Monate; Logins/Sessions mit IP + GeoIP aus lokaler DB 90 Tage; **nicht** gespeichert: Audio, Terminal-Inhalte; IPs sehen nur Admins; Kontakt info@oe5xrx.org; Link „Vollständige Datenschutzerklärung“ → `PRIVACY_POLICY_URL` (`target="_blank" rel="noopener"`); Versionsanzeige „Version {{ version }}“; Form `method="post"` mit `{% csrf_token %}`, hidden `next`, Button „Zur Kenntnis genommen“; Logout-Link als Ausweg.

POST-Verhalten: `version = settings.PRIVACY_POLICY_VERSION`; wenn `version > 0` und `user.privacy_ack_version < version` → Felder setzen, `save(update_fields=["privacy_ack_version", "privacy_ack_at"])`, `AccountAuditLog.log(event_type=PRIVACY_ACKNOWLEDGED, actor=user, target_user=user, message=f"privacy policy v{version} acknowledged", ip_address=client_ip(request), details={"version": version})`. Bereits bestätigt → kein zweiter Audit-Eintrag. Danach Redirect auf `_safe_next`.

- [ ] **Step 1: Failing tests**
```python
def test_get_renders_summary_and_link(client, member_user, settings):
    settings.PRIVACY_POLICY_VERSION = 1
    r = client.get(reverse("accounts:privacy_ack") + "?next=/en/")
    assert r.status_code == 200
    assert "https://oe5xrx.org/docs/datenschutz.html" in r.text and "Zur Kenntnis genommen" in r.text
    assert 'value="/en/"' in r.text  # next im hidden field
def test_post_records_ack_and_audit(...):
    r = client.post(url, {"next": "/en/stations/"})
    member_user.refresh_from_db()
    assert member_user.privacy_ack_version == 1 and member_user.privacy_ack_at is not None
    row = AccountAuditLog.objects.get(event_type="privacy_acknowledged")
    assert row.actor == row.target_user == member_user and row.details == {"version": 1}
    assert "@" not in row.message
    assert r.status_code == 302 and r.url == "/en/stations/"
def test_double_post_single_audit_row(...)  # count == 1
@pytest.mark.parametrize("bad", ["//evil.example/", "https://evil.example/", "javascript:alert(1)"])
def test_unsafe_next_falls_back(..., bad):   # r.url == reverse("dashboard:index")
def test_post_returns_to_sso_authorize_with_query(...)  # next="/sso/authorize/?client_id=abc&state=xyz" → r.url identisch
def test_after_ack_dashboard_reachable(...)  # Middleware lässt durch
def test_get_requires_login(client)          # anonym → Login-Redirect
```
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** `Skill("frontend-design")`, dann View + Template.
- [ ] **Step 4:** Tests grün; `python scripts/check_template_comments.py && python scripts/check_color_tokens.py` grün; Seite einmal manuell im Dev-Server (DE + EN) ansehen.
- [ ] **Step 5:** Commit `feat(privacy): acknowledgement page`.

---

### Task 4: Footer-Link „Datenschutz“ auf allen Seiten

**UI-Arbeit: `Skill("frontend-design")` zuerst**, dezent im bestehenden Stil.

**Files:**
- Create: `templates/includes/privacy_footer.html`
- Modify: `apps/accounts/context_processors.py` (neue Funktion), `config/settings/base.py` TEMPLATES `context_processors` (+ `"apps.accounts.context_processors.privacy"`)
- Modify: `templates/base.html` (Include **nach** `{% endblock body %}`, damit auch Seiten mit eigenem `body`-Block — `set_password.html`, `password_reset_request.html`, `privacy_ack.html` — ihn bekommen), `apps/accounts/templates/accounts/login.html` (eigenständig, nicht `extends` → dort explizit includen)
- Test: `tests/test_privacy_footer.py`

**Interfaces:**
- Produces: `privacy(request) -> dict` mit `PRIVACY_POLICY_URL`, `PRIVACY_POLICY_VERSION`.
- Footer wird nur gerendert, wenn `PRIVACY_POLICY_VERSION > 0` (siehe Ambiguität 2).

- [ ] **Step 1: Failing tests** (`settings.PRIVACY_POLICY_VERSION = 1`, User hat bestätigt)
```python
@pytest.mark.parametrize("name", ["accounts:login", "accounts:password_reset_request"])
def test_footer_on_anonymous_pages(client, settings, name):
    assert 'href="https://oe5xrx.org/docs/datenschutz.html"' in client.get(reverse(name)).text
def test_footer_on_dashboard(...)
def test_footer_on_privacy_ack_page(...)
def test_footer_hidden_when_version_zero(...)
```
- [ ] **Step 2:** FAIL. **Step 3:** implementieren. **Step 4:** grün + Template-Checks. **Step 5:** Commit `feat(privacy): footer link`.

---

### Task 5: Keine E-Mail/IP mehr in Audit-Meldungen + `scrub_message`

**Files:**
- Modify (exakte Call-Sites, `origin/main` @ 4dad823):

| Datei:Zeile | Event | heute | neu |
|---|---|---|---|
| `apps/accounts/views.py:198` | EMAIL_VERIFY_REQUESTED | `f"{user.email} → {new_email}"` | `"email change requested"` |
| `apps/accounts/views.py:508` | EMAIL_VERIFIED | `f"{old_email} → {new_email}"` | `"email changed"` |
| `apps/accounts/views.py:586` | PASSWORD_RESET_REQUESTED | `f"to {email} from {ip}"` | `"reset link sent"` |
| `apps/accounts/views.py:608` | PASSWORD_RESET_RATE_LIMITED | `f"{reason} {email} from {ip}"` | `f"rate limited ({reason})"` |
| `apps/accounts/views.py:669` | WELCOME_TOKEN_SENT (resend) | `f"resend to {user.email}"` | `"welcome link re-sent"` |
| `apps/accounts/views.py:780` | USER_CREATED | `f"{user.username} <{user.email}>"` | `user.username` |
| `apps/accounts/views.py:800` | WELCOME_TOKEN_SENT | `f"to {user.email}"` | `"welcome link sent"` |
| `apps/accounts/views.py:1019` | USER_SOFT_DELETED | `f"{target.username} <{target.email}>"` | `target.username` |
| `apps/accounts/views.py:1122` | USER_RESTORED | `f"{target.username} <{target.email}>"` | `target.username` |
| `apps/accounts/views.py:1222` | USER_HARD_PURGED | `f"{username} <{email}> (purged …)"` | `f"{username} (purged after soft-delete on {deleted_at:%Y-%m-%d})"`; Variable `email` + Kommentar ~Z. 1197–1199 anpassen |
| `apps/sso/permissions.py:278` | LOGIN_SUCCESS | `f"Token issued. UA={ua[:80]} City={city…}"` | `"Token issued."` (UA/City leben nur in `TokenSession`, 90-d-Regel) |
| `apps/sso/views.py:509` | SESSION_REVOKED | `… f"from {session.ip_address} ({session.city…})"` | `f"Session {session.pk} revoked. Issued {session.issued_at.isoformat()}."` |

  Bewusst **nicht** geändert: `apps/api/views.py:114` (Stations-IP-Wechsel, Geräte-IP, `user=None`, System-Eintrag — Ambiguität 4). Flash-Messages (`messages.info(... %(email)s)`) sind kein Audit → bleiben.
- Create: `apps/audit/scrub.py`
- Test: `tests/test_audit_message_pii.py`, `tests/test_audit_scrub.py`

**Interfaces:**
- Produces: `scrub_message(text: str) -> str` (rein, idempotent), Konstanten `EMAIL_PLACEHOLDER = "[email]"`, `IP_PLACEHOLDER = "[ip]"`, `SCRUB_PREFILTER: Q` (grobe DB-Vorauswahl: `message__contains="@"` | `message__regex=r"[0-9]\.[0-9]+\.[0-9]"` | `message__contains="::"` | `message__regex=r"[0-9A-Fa-f]:[0-9A-Fa-f]"` | `message__startswith="Token issued. UA="` | `message__contains=" from "`).

Algorithmus `scrub_message` (Reihenfolge fix):
1. Legacy LOGIN_SUCCESS: `^Token issued\. UA=.*$` (DOTALL) → `"Token issued."`.
2. Legacy SESSION_REVOKED: ` from \S+ \([^)]*\)` → `""` (entfernt IP **und** Stadt).
3. E-Mail: `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}` → `[email]`.
4. IP: Kandidaten `(?<![\w.:])[0-9A-Fa-f:.]{2,}(?![\w.:])` die `.` oder `:` enthalten; ersetzen **nur**, wenn `ipaddress.ip_address(token)` gültig ist (verhindert Fehltreffer auf Uhrzeiten/Versionen).

- [ ] **Step 1: Failing tests**
```python
# test_audit_scrub.py
@pytest.mark.parametrize("raw,clean", [
    ("to oe5abc@example.org from 192.0.2.10", "to [email] from [ip]"),
    ("OE5ABC <a.b+c@mail.example.at>", "OE5ABC <[email]>"),
    ("user 2001:db8::1 and ::1", "user [ip] and [ip]"),
    ("Token issued. UA=Mozilla/5.0 (X11) City=Linz", "Token issued."),
    ("Session 7 revoked. Issued 2026-01-01T12:30:45+00:00 from 192.0.2.1 (Linz)",
     "Session 7 revoked. Issued 2026-01-01T12:30:45+00:00"),
    ("firmware v1.2.3.4 at 12:30:45", "firmware v1.2.3.4 at 12:30:45"),
    ("version 1.2.3", "version 1.2.3"),
    ("Policy grant_required -> open. 2 active session(s)", "Policy grant_required -> open. 2 active session(s)"),
])
def test_scrub_message(raw, clean): assert scrub_message(raw) == clean
def test_scrub_is_idempotent(): assert scrub_message(scrub_message(x)) == scrub_message(x)

# test_audit_message_pii.py — je Call-Site ein Test über den echten View/Flow:
def test_password_reset_audit_has_no_email_or_ip(client, member_user): ...
    # row = AccountAuditLog.objects.get(event_type="password_reset_requested")
    # assert member_user.email not in row.message and "127.0.0.1" not in row.message
    # assert row.ip_address == "127.0.0.1"
def test_password_reset_rate_limited_audit_has_no_email(...)
def test_user_create_audit_has_no_email(...)       # + welcome_token_sent
def test_resend_welcome_audit_has_no_email(...)
def test_email_verify_requested_and_verified_have_no_email(...)
def test_soft_delete_restore_purge_audit_have_no_email(...)  # purge message == f"{username} (purged after soft-delete on YYYY-MM-DD)"
def test_sso_login_success_message_has_no_ua_city(...)       # über TokenSession-Issuance-Hook; existierende Tests in tests/test_sso_audit.py als Vorlage
def test_sso_session_revoked_message_has_no_ip_city(...)
```
- [ ] **Step 2:** FAIL. Bestehende Tests, die alte Meldungstexte asserten, suchen: `grep -rn "<.*@\|from 127\|UA=\|City=" tests/` und mit anpassen.
- [ ] **Step 3:** Call-Sites ändern, `scrub.py` implementieren.
- [ ] **Step 4:** `pytest tests/test_audit_scrub.py tests/test_audit_message_pii.py tests/test_sso_audit.py tests/test_account_audit_log.py tests/test_user_update_create_audit.py -q` grün.
- [ ] **Step 5:** Commit `fix(audit): no email/ip/ua in audit messages`.

---

### Task 6: Retention-Kern `apps/audit/retention.py`

**Files:**
- Create: `apps/audit/retention.py`
- Modify: `apps/sso/models.py` (`TokenSessionQuerySet.stale(now=None)`), `apps/sso/management/commands/prune_token_sessions.py` (nutzt `TokenSession.objects.stale(now)`, Verhalten/Output unverändert — `tests/test_sso_sessions.py:331–348` bleibt grün)
- Test: `tests/test_retention.py`

**Interfaces:**
- Consumes: Task 1 Settings, Task 5 `scrub_message`, `SCRUB_PREFILTER`.
- Produces:
```python
@dataclass(frozen=True)
class RetentionConfig:
    now: datetime; audit_days: int; ip_days: int; enforce: bool; batch_size: int
    @property audit_cutoff -> datetime; @property ip_cutoff -> datetime
    @classmethod
    def from_settings(cls, *, now: datetime | None = None, dry_run: bool = False) -> "RetentionConfig"
        # enforce = settings.RETENTION_ENFORCE and not dry_run; Tage < 1 zählen als 1

@dataclass
class RetentionResult:
    enforce: bool
    counts: dict[str, int]      # Kategorie -> betroffene Zeilen (Dry-Run: würde-betroffen)
    errors: list[str]           # Kategorien, deren Schritt eine Exception warf

def held_user_ids() -> frozenset[int]   # Phase-2-Hook (Löschsperre); jetzt immer frozenset()
def run_retention(cfg: RetentionConfig) -> RetentionResult
TokenSessionQuerySet.stale(self, now: datetime | None = None) -> QuerySet  # exakt die 3 Q-Kriterien aus prune_token_sessions
PERSONAL_STATION_EVENT_TYPES: frozenset[str]
```
- Interne Helper: `_apply(qs, cfg, *, update: dict | None = None) -> int` — Dry-Run `qs.count()`; Enforce in Batches: `pks = list(qs.values_list("pk", flat=True)[:cfg.batch_size])` → `Model.objects.filter(pk__in=pks).delete()` bzw. `.update(**update)`, Rückgabe = Summe `len(pks)` (nicht Kaskaden-Zähler). Update-Querysets müssen ihre eigenen Treffer ausschließen (Filter auf „Feld noch gesetzt“), sonst Endlosschleife. `_exclude_held(qs, held, *fk_fields) -> QuerySet` — No-op bei leerem `held`.
- `run_retention` ruft jeden Schritt in eigenem `try/except` (+ `logger.exception`), Fehler → `errors`, übrige Schritte laufen weiter.

Kategorien (Schlüssel in `counts`, diese Reihenfolge):

| Schlüssel | Queryset (vor `_exclude_held`) | Aktion |
|---|---|---|
| `station_audit_deleted` | `StationAuditLog`, `created_at < audit_cutoff` & (`user` gesetzt **oder** `event_type ∈ PERSONAL_STATION_EVENT_TYPES`); held: `user` | delete |
| `account_audit_deleted` | `AccountAuditLog`, `created_at < audit_cutoff` (alle Zeilen, Ambiguität 3); held: `actor`, `target_user` | delete |
| `sso_audit_deleted` | `SsoAuditLog`, analog | delete |
| `terminal_sessions_deleted` | `TerminalSession`, `ended_at < audit_cutoff` **oder** (`ended_at` NULL & `started_at < audit_cutoff`); held: `user` | delete |
| `station_audit_ip_cleared` / `account_audit_ip_cleared` / `sso_audit_ip_cleared` | `created_at < ip_cutoff`, `ip_address__isnull=False` | `ip_address=None` |
| `token_session_client_cleared` | `TokenSession`, `issued_at < ip_cutoff` & (ip gesetzt \| ua≠"" \| country≠"" \| city≠""); held: `user` | `ip_address=None, user_agent="", country_code="", city=""` |
| `account_token_ip_cleared` | `AccountToken`, `created_at < ip_cutoff`, `ip_created` gesetzt; held: `user` | `ip_created=None` |
| `account_tokens_expired_deleted` | `AccountToken`, `expires_at < now`; held: `user` | delete |
| `token_sessions_pruned` | `TokenSession.objects.stale(now)`; held: `user` | delete |
| `django_sessions_cleared` | Dry-Run: `Session.objects.filter(expire_date__lt=now).count()`; Enforce: Zahl vorher zählen, dann `call_command("clearsessions")` | |
| `oauth_tokens_cleared` | Dry-Run: `AccessToken.objects.filter(expires__lt=now).count()` (Näherung, im Log so benannt); Enforce: zählen, dann `call_command("cleartokens")` | |
| `audit_messages_scrubbed` | `AccountAuditLog` + `SsoAuditLog` mit `SCRUB_PREFILTER`; in Python `scrub_message`, nur geänderte zählen; Enforce: `bulk_update(["message"], batch_size)` (keine Held-Ausnahme: IP bleibt im Feld `ip_address`) | |

`PERSONAL_STATION_EVENT_TYPES` = `CONTROL_LOCK_ACQUIRED, CONTROL_LOCK_RELEASED, CONTROL_LOCK_TRANSFERRED, CONTROL_LOCK_PREEMPTED, CONTROL_COMMAND, CONTROL_PTT, STATION_ASSIGNMENT_CREATED, STATION_ASSIGNMENT_REVOKED` (Terminal hat kein Station-Event, sondern `TerminalSession`).

Hinweis für Tests: `created_at` ist `auto_now_add` → per `Model.objects.filter(pk=…).update(created_at=…)` zurückdatieren. Feste `now` per `RetentionConfig(now=…)` injizieren.

- [ ] **Step 1: Failing tests** (`tests/test_retention.py`, Helper `_cfg(enforce=True, now=NOW)`)
```python
def test_dry_run_counts_but_changes_nothing():
    # alte PTT-Zeile + alte IP → counts["station_audit_deleted"] == 1, counts["station_audit_ip_cleared"] >= 1
    # Zeile existiert danach noch, ip_address unverändert
def test_station_audit_deletes_only_personal_rows():
    # 366 d: user gesetzt (UPDATED) → weg; user=None + CONTROL_PTT (gepurgter User) → weg
    # 366 d: user=None + STATUS_CHANGE → bleibt; 364 d mit user → bleibt
def test_account_and_sso_audit_deleted_after_365_days()
def test_terminal_sessions_closed_and_orphaned_deleted()   # ended 366 d → weg; ended 10 d → bleibt; ended NULL+started 400 d → weg; active neu → bleibt
def test_ip_cleared_after_90_days_row_kept():              # 91 d: ip None, Zeile da, message unverändert; 89 d: ip bleibt — für alle 3 Audit-Tabellen
def test_token_session_client_fields_cleared()             # ip/ua/country/city leer nach 91 d
def test_account_token_ip_cleared_and_expired_deleted()
def test_prune_token_sessions_reused()                     # stale() == alte Command-Semantik; Command-Output "Pruned N TokenSession row(s)." unverändert
def test_expired_django_sessions_cleared()
def test_audit_messages_scrubbed_and_idempotent():         # Legacy "to a@b.at from 192.0.2.1" → "to [email] from [ip]"; zweiter Lauf: count 0
def test_held_users_are_skipped(monkeypatch):
    monkeypatch.setattr(retention, "held_user_ids", lambda: frozenset({member_user.pk}))
    # alte Zeilen + alte IPs dieses Users bleiben (beide Fristen), andere werden bearbeitet
def test_run_is_idempotent()                              # zweiter Enforce-Lauf: alle counts 0
def test_batching(settings)                               # batch_size=2, 5 Zeilen → alle 5 weg, count 5
def test_step_error_is_isolated(monkeypatch)              # ein Schritt wirft → "errors" enthält Kategorie, andere counts vorhanden
def test_enforce_false_setting_forces_dry_run(settings)   # RETENTION_ENFORCE=False → from_settings().enforce is False
```
- [ ] **Step 2:** FAIL. **Step 3:** implementieren. **Step 4:** `pytest tests/test_retention.py tests/test_sso_sessions.py -q` grün. **Step 5:** Commit `feat(audit): retention engine`.

---

### Task 7: Command `enforce_retention` + Zeitplan

**Files:**
- Create: `apps/audit/management/__init__.py`, `apps/audit/management/commands/__init__.py`, `apps/audit/management/commands/enforce_retention.py`
- Modify: `deploy/docker-compose.prod.yml` (Service `retention` nach `history-maintainer`, gleiches Schema), `docker-compose.yml` (Dev-Service analog `history-maintainer` ~Z. 89)
- Modify: `CLAUDE.md` des Repos — kurzer Abschnitt „Datenschutz / Retention (`apps/audit/retention.py`)“: Settings, Dry-Run-Default, eigener Compose-Service, „das servers-Repo braucht ihn ebenfalls“, Legal-Hold-Hook.
- Test: `tests/test_enforce_retention_command.py`

**Interfaces:**
- Consumes: `RetentionConfig.from_settings`, `run_retention`, `RetentionResult` (Task 6).
- Produces: `manage.py enforce_retention [--dry-run] [--loop] [--interval SECONDS]` (Default-Intervall `86400`, `_positive_int` wie `apps/history/management/commands/history_maintain.py`); Loop mit `close_old_connections()` pro Iteration.
- Ausgabe (eine Zeile, stdout; bei `errors` stderr) + gleichlautend `logger.info` auf Logger `apps.audit.retention`:
  `enforce_retention[DRY-RUN|ENFORCE]: station_audit_deleted=N account_audit_deleted=N … errors=none`
  Bei Dry-Run wegen Setting zusätzlich Hinweis `(RETENTION_ENFORCE is false)`.

Compose-Service (prod):
```yaml
  retention:
    image: ghcr.io/oe5xrx/station-manager:latest
    restart: unless-stopped
    command: python manage.py enforce_retention --loop --interval 86400
    env_file: [.env]
    environment:
      - DJANGO_SETTINGS_MODULE=config.settings.prod
    depends_on: {db: {condition: service_healthy}, redis: {condition: service_started}}
```

- [ ] **Step 1: Failing tests**
```python
def test_command_dry_run_flag_changes_nothing(settings):   # RETENTION_ENFORCE=True + --dry-run → "DRY-RUN" in out, Zeilen bleiben
def test_command_defaults_to_dry_run_when_not_enforced(settings)  # "RETENTION_ENFORCE is false" in out
def test_command_enforces_when_setting_true(settings)      # "ENFORCE" in out, alte Zeile weg
def test_output_lists_every_category()                     # alle Schlüssel aus Task 6 in der Zeile
def test_interval_must_be_positive()                       # CommandError/SystemExit bei --interval 0
def test_prod_compose_has_retention_service():
    svc = yaml.safe_load(Path("deploy/docker-compose.prod.yml").read_text())["services"]["retention"]
    assert "enforce_retention --loop --interval 86400" in svc["command"]
```
- [ ] **Step 2:** FAIL. **Step 3:** implementieren. **Step 4:** grün; manuell `python manage.py enforce_retention` gegen Dev-DB → eine Zählzeile. **Step 5:** Commit `feat(audit): enforce_retention command + compose service`.

---

### Task 8: Gesamt-Verifikation + PR

- [ ] **Step 1:** Volle CI-Gates (siehe Global Constraints) → alles grün, `makemigrations --check` ohne Änderungen.
- [ ] **Step 2:** E2E manuell im Dev-Server mit `PRIVACY_POLICY_VERSION=1`: Login → Hinweis → Bestätigen → Dashboard; Logout/Login → kein Hinweis; `PRIVACY_POLICY_VERSION=2` → Hinweis erneut; OIDC-Authorize-URL direkt aufrufen → Hinweis → zurück zur Authorize-Seite; Agent-Heartbeat (`seed_dev_station`) läuft unbeeinträchtigt.
- [ ] **Step 3:** PR öffnen. PR-Text nennt explizit die Deploy-Reihenfolge: Deploy mit `PRIVACY_POLICY_VERSION=0` + `RETENTION_ENFORCE=false` → Peter prüft ersten Dry-Run-Log → nach Vorstandsfreigabe und Veröffentlichung des Texts `PRIVACY_POLICY_VERSION=1`, dann `RETENTION_ENFORCE=true`.

### Task 9: Begleit-PR im Repo `servers` (Pflicht, sonst läuft der Job in Prod nicht)

Produktion läuft aus `servers/services/station_manager/docker-compose.yml`, nicht aus `deploy/docker-compose.prod.yml`.

**Files:** `servers/services/station_manager/docker-compose.yml`
- [ ] Im Anchor `x-station-manager-env` (~Z. 38): `PRIVACY_POLICY_VERSION: ${PRIVACY_POLICY_VERSION:-0}`, `PRIVACY_POLICY_URL: ${PRIVACY_POLICY_URL:-https://oe5xrx.org/docs/datenschutz.html}`, `RETENTION_ENFORCE: ${RETENTION_ENFORCE:-false}`, `RETENTION_AUDIT_DAYS: ${RETENTION_AUDIT_DAYS:-365}`, `RETENTION_IP_DAYS: ${RETENTION_IP_DAYS:-90}`.
- [ ] Service `retention` analog `history-maintainer` (~Z. 285: `environment: *station-manager-env`, `networks: [station_manager-internal]`, `depends_on: init: service_completed_successfully`), Kommentar wie dort.
- [ ] `.github/workflows/prune-token-sessions.yml` bleibt (idempotent, doppelt schadet nicht); im PR-Text erwähnen, dass er nach Enforce-Freischaltung entfallen kann.
- [ ] Erst mergen, nachdem das station-manager-Image mit dem Command veröffentlicht ist.

---

## Follow-up (Phase 2, nicht in diesem Plan)

- Austritt in zwei Stufen (Stufe 1 sofort: Profil leeren, nur Rufzeichen bleibt; Stufe 2 nach 365 d im Retention-Job), Umbau Papierkorb `apps/accounts/views.py:1139–1225`, Migration bestehender soft-gelöschter User.
- „Deaktivieren“ (umkehrbar) vs. „Austritt“ (endgültig) als getrennte Aktionen.
- Löschsperre: Flag + Grund + Admin am User; `held_user_ids()` in `apps/audit/retention.py` liest es (gilt für beide Fristen). Achtung: `cleartokens` kaskadiert über `RefreshToken` auf `TokenSession` und ignoriert die Sperre — dann einschränken.
- Rufzeichen-Sperre für ausgetretene Accounts (12 Monate), Autor-FKs (`StationLogEntry`, `StationPhoto`, `Alert.acknowledged_by`) in Stufe 2 auf NULL.
- Backup-Retention (`servers/backup/restic-backup.sh`: `--keep-yearly 3` streichen).
