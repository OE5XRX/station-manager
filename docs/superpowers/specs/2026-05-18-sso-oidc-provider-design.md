# SSO / OIDC-Provider im Station-Manager — Design

**Status:** Draft, brainstormed 2026-05-18.
**Ziel:** Den station-manager so erweitern, dass er als zentraler OpenID-Connect-Provider für andere Vereins-Apps (InvenTree, Grafana, Nextcloud, Wiki, …) dient. Eine User-Datenbank, eine Login-Page, ein Ort für Zugriffsverwaltung.

---

## 1. Architektur

### 1.1 Neue Django-App

`apps/sso` wird ein dünner Layer auf [django-oauth-toolkit (DOT)](https://django-oauth-toolkit.readthedocs.io/). DOT ist Industry-Standard für OIDC-Provider in Django, breit genutzt gegen InvenTree/Grafana/Nextcloud.

**Was wir selbst schreiben (`apps/sso`):**
- `models.py` — nur `AppGrant` (User × Application = "darf rein")
- `oidc_claims.py` — Hook-Function für custom Claims (`groups`, `apps`, `name`, ...)
- `permissions.py` — Validator-Hook der prüft "darf User in *diese* App?" vor Token-Ausgabe
- `views.py` — Custom-Admin-Views für AppGrant-Management (Toggle, Übersicht)
- `admin.py` — Django-Admin-Erweiterungen für `Application`-CRUD
- Templates für SSO-Übersichts-Page und User-Detail-Erweiterung

**Was DOT mitbringt (nicht von uns):**
- Models: `Application`, `AccessToken`, `RefreshToken`, `Grant`, `IDToken`
- Endpoints: `/sso/authorize/`, `/sso/token/`, `/sso/userinfo/`, `/sso/revoke_token/`, `/sso/.well-known/openid-configuration`, `/sso/.well-known/jwks.json`, `/sso/logout/`
- Auth-Code-Flow mit PKCE, Refresh Tokens, RP-initiated Logout, JWKS-Endpoint

### 1.2 Integration in `config/`

- `INSTALLED_APPS` += `["oauth2_provider", "apps.sso"]`
- `config/urls.py`: neue Route `path("sso/", include("oauth2_provider.urls", namespace="oauth2_provider"))` **außerhalb** von `i18n_patterns` — OIDC-Endpoints brauchen stabile URLs ohne Locale-Prefix.
- `config/settings/base.py`: `OAUTH2_PROVIDER` Dict mit OIDC-Konfig, RSA-Keypair-Pfad, Token-Lifetimes, Custom-Hooks.

### 1.3 Bestehende Auth-Pfade bleiben unverändert

- **Browser-Login der Web-UI:** bleibt Session-basiert (`django.contrib.auth`).
- **Station-Agent Ed25519-Auth (`DeviceKeyAuthentication`):** komplett separater Pfad, null Auswirkung.
- **Neu:** OIDC-Layer auf `/sso/*` für externe RP-Apps.

### 1.4 URL-Präfix

`/sso/` (nicht DOT-Default `/o/`). Begründung: Passt zum App-Namen, ist für Admins/User sofort verständlich (Single Sign-On), keine Jargon-Verwirrung (OIDC vs. OAuth).

---

## 2. Datenmodell

### 2.1 Eigene Models

```python
class AppGrant(models.Model):
    """Erlaubt einem User Zugriff auf eine registrierte OIDC-Application.

    Wenn kein aktiver AppGrant existiert → User bekommt kein Token für
    diese App, OIDC-Authorize-Endpoint antwortet mit access_denied.
    """
    user = ForeignKey(User, on_delete=CASCADE, related_name="app_grants")
    application = ForeignKey(
        "oauth2_provider.Application",
        on_delete=CASCADE,
        related_name="grants",
    )
    granted_at = DateTimeField(auto_now_add=True)
    granted_by = ForeignKey(User, on_delete=SET_NULL, null=True,
                            related_name="granted_app_grants")
    revoked_at = DateTimeField(null=True, blank=True)  # soft delete

    class Meta:
        constraints = [
            UniqueConstraint(
                fields=["user", "application"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_active_grant_per_user_per_app",
            ),
        ]
        indexes = [Index(fields=["application", "revoked_at"])]
```

**Begründungen:**
- **Soft-Delete via `revoked_at`** statt Hard-Delete: Audit-Spur bleibt erhalten ("Peter hatte mal Zugriff, wurde am DD entzogen").
- **Partial Unique Index** verhindert doppelte aktive Grants, erlaubt aber Re-Granting nach Revoke.
- **`granted_by`** für Accountability.

### 2.2 DOT-Models (mitnutzen, nicht selbst definieren)

- `oauth2_provider.Application` — pro RP-App (client_id, secret, redirect_uris, …). FK darauf reicht uns.
- `oauth2_provider.AccessToken`, `RefreshToken`, `Grant`, `IDToken` — Token-Lifecycle transparent.

### 2.3 User-Model-Refactor: Role → Django Groups

**Heute:** `User.role` ist CharField mit `TextChoices` (admin/operator/member), exakt eine Rolle pro User.

**Neu:** `User.role`-Field entfällt. Stattdessen Django's eingebautes `auth.Group` M2M.

```python
class User(AbstractUser):
    @cached_property
    def is_admin(self):
        return self.groups.filter(name="admin").exists()

    @cached_property
    def is_operator(self):
        return self.groups.filter(name="operator").exists()

    @cached_property
    def is_staff_member(self):  # "admin OR operator"
        return self.groups.filter(name__in=["admin", "operator"]).exists()
```

**Effekte:**
- Multi-Role wird automatisch möglich (User kann z.B. `admin` UND `techniker` sein).
- Neue Rolle anlegen = Django-Admin → Groups → Add. Kein Code, keine Migration, kein Deploy.
- Bestehender Code (`if user.is_admin:`) funktioniert unverändert über die Properties.
- ~15–20 Call-Sites mit `user.role == "admin"` (oder `in ("admin", "operator")`) werden auf `user.is_admin` / `user.is_staff_member` umgestellt.

### 2.4 Custom OIDC-Claims

```python
# apps/sso/oidc_claims.py
def add_claims(claims, user, request):
    """Hook für DOT's OIDC_USERINFO_HOOK.

    Wird beim ID-Token-Build und beim UserInfo-Request aufgerufen.
    """
    claims["preferred_username"] = user.username
    claims["email"] = user.email
    claims["email_verified"] = True   # interne Vereinsemails
    claims["name"] = user.get_full_name() or user.username
    claims["locale"] = user.language  # "de" / "en"
    claims["groups"] = list(user.groups.values_list("name", flat=True))
    return claims
```

`groups` ist immer ein Array (auch bei nur einer Group) — Standard-OIDC-Konvention, was InvenTree/Grafana/Nextcloud alle als Array erwarten.

### 2.5 Zugriffs-Validator

```python
# apps/sso/permissions.py
def user_can_access(user, application) -> bool:
    """Eingehakt in DOT's Authorization-Flow.

    Returns False → access_denied (RFC-konformes Error-Redirect).
    """
    if not user.is_active:
        return False
    return AppGrant.objects.filter(
        user=user, application=application, revoked_at__isnull=True
    ).exists()
```

Eingebunden via DOT's `OAUTH2_VALIDATOR_CLASS` — wir erben von DOT's `OAuth2Validator` und überschreiben den Authorization-Request-Validator. Exakte Override-Methode (`validate_authorization_request` vs. `_get_user_can_authorize` vs. Custom-Middleware vor `AuthorizationView`) entscheidet die Plan-Phase nach DOT-Quellenstudium.

---

## 3. OIDC-Flow & Endpunkte

### 3.1 Unterstützte Flows

**Nur Authorization Code mit PKCE.** Keine impliziten Flows, kein Password-Grant, kein Client-Credentials für Login. PKCE Pflicht auch für Confidential Clients.

### 3.2 Token-Lifetimes

| Token | Lifetime | Begründung |
|---|---|---|
| Access Token | 1 h | Standard. UserInfo + RP-API-Zugriff. |
| ID Token | 1 h | Parallel zum Access Token. |
| Refresh Token | 14 Tage | Komfort vs. Offboarding-Latenz. |
| Authorization Code | 60 s | RFC 6749 empfiehlt sehr kurz. |

### 3.3 Login-Flow (Happy Path)

```
1. User → InvenTree:   "ich will rein"
2. InvenTree → Browser: 302 → GET /sso/authorize/
                        ?response_type=code
                        &client_id=inventree-prod
                        &redirect_uri=https://parts.oe5xrx.org/oidc/callback
                        &scope=openid profile email groups
                        &state=<random>
                        &code_challenge=<sha256(verifier)>
                        &code_challenge_method=S256
3. station-manager → Browser:
                        nicht eingeloggt → bestehende accounts:login Seite
4. User gibt Credentials ein → Standard-Django-Login.
   django-axes Brute-Force-Schutz greift unverändert.
5. station-manager → AppGrant-Check:
                        - kein Grant → 302 zurück mit ?error=access_denied
                        - Grant     → weiter
6. station-manager → Consent-Page (nur beim ersten Login dieses Users
                        für DIESE App; danach gespeichert).
7. station-manager → Browser: 302 → InvenTree-callback
                        ?code=<authcode>&state=<unchanged>
8. InvenTree (server-side):
                        POST /sso/token/
                          grant_type=authorization_code
                          code=<authcode>
                          redirect_uri=<same>
                          code_verifier=<original>
                          + Basic Auth (client_id : client_secret)
9. station-manager → InvenTree:
                        { access_token, id_token, refresh_token,
                          expires_in, token_type: "Bearer" }
10. InvenTree extrahiert User aus id_token → JIT-Provisioning →
    eigene Session → User sieht InvenTree-Startseite.
```

Falls User bereits am station-manager eingeloggt (Browser-Session): Schritte 3+4 entfallen, beim Re-Login mit derselben App auch Consent. → Echtes SSO-Erlebnis.

### 3.4 ID-Token-Inhalt

```json
{
  "iss": "https://ham.oe5xrx.org/sso",
  "sub": "42",                          // User-PK, stabil
  "aud": "inventree-prod",
  "exp": 1715000000,
  "iat": 1714996400,
  "auth_time": 1714996400,
  "preferred_username": "peterb",
  "email": "peter@oe5xrx.org",
  "email_verified": true,
  "name": "Peter Buchegger",
  "locale": "de",
  "groups": ["operator", "techniker"]
}
```

Signiert mit RS256 (RSA-2048, Private-Key am Server, Public-Key über JWKS).

Bewusst weggelassen: `is_active` als Claim. Wäre Tautologie — inaktive User kriegen erst gar kein Token (Validator-Hook lehnt sie ab). Plus: Risiko dass RPs den Claim missinterpretieren ("inactive User aber Token? schwammig").

### 3.5 Logout-Flow (RP-initiated)

```
1. User in InvenTree klickt Logout
2. InvenTree → Browser: 302 → /sso/logout/
   ?id_token_hint=<jwt>
   &post_logout_redirect_uri=https://parts.oe5xrx.org/goodbye
3. station-manager:
   - validiert id_token_hint
   - validiert post_logout_redirect_uri gegen registrierte URIs (Open-Redirect-Schutz)
   - löscht Browser-Session am station-manager
   - revokiert AccessToken/RefreshToken in DB
4. station-manager → Browser: 302 → post_logout_redirect_uri
```

**Wichtig:** Logout am station-manager loggt den User nicht aus den **anderen** Apps aus. Andere Apps merken's beim nächsten Token-Refresh (max Refresh-Token-Lifetime). Front-Channel-Single-Logout über alle Apps gleichzeitig ist V2-Kandidat (siehe Section 7).

### 3.6 Error-Pfade (RFC-konform)

- Kein AppGrant → `error=access_denied`, Redirect zurück zur RP
- User inaktiv → `error=access_denied`, Redirect zurück
- Unbekannte Application oder falsche `redirect_uri` → 400 Bad Request, **kein** Redirect (Open-Redirect-Vulnerability)
- Code abgelaufen / Replay → 400, `invalid_grant`
- PKCE-Verifier mismatch → 400, `invalid_grant`

### 3.7 Discovery-Endpoint

`/sso/.well-known/openid-configuration` wird von DOT generiert. Inhalt grob:

```json
{
  "issuer": "https://ham.oe5xrx.org/sso",
  "authorization_endpoint": "https://ham.oe5xrx.org/sso/authorize/",
  "token_endpoint": "https://ham.oe5xrx.org/sso/token/",
  "userinfo_endpoint": "https://ham.oe5xrx.org/sso/userinfo/",
  "jwks_uri": "https://ham.oe5xrx.org/sso/.well-known/jwks.json",
  "end_session_endpoint": "https://ham.oe5xrx.org/sso/logout/",
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "refresh_token"],
  "scopes_supported": ["openid", "profile", "email", "groups"],
  "code_challenge_methods_supported": ["S256"],
  "id_token_signing_alg_values_supported": ["RS256"],
  "subject_types_supported": ["public"]
}
```

RPs lesen das automatisch → in InvenTree etc. wird nur Discovery-URL + Client-ID + Client-Secret konfiguriert.

---

## 4. Admin-UX

### 4.1 Application-Registrierung → Django-Admin

Lebt unter `/admin/sso/application/`. Begründung:
- Wird **selten** gemacht (einmal pro RP-App, ever).
- `client_secret` ist sensibles Material; Django-Admin-Permissions reichen.
- Pattern-Match: andere seltene Konfig (AlertRule, ModuleType) lebt auch im Django-Admin.

**ModelAdmin-Customizings:**
- Nach Create wird `client_secret` **einmalig** im Klartext angezeigt (Pattern existiert bei `StationGenerateKeyView` für DeviceKey-Private-Key). Danach nur noch Hash.
- Liste: `name`, `client_id`, `redirect_uris`, `# aktive Grants`, `created_at`.
- Detail: `name`, `redirect_uris` (mehrzeilig), `allowed_scopes` (Multi-Select), `client_type` (Default: Confidential), `authorization_grant_type` (locked auf `authorization-code`).

### 4.2 User → App-Zugriff → Custom-UI auf User-Detail-Page

Erweitert `apps/accounts/templates/accounts/profile.html` bzw. `user_form.html`:

```
┌──────────────────────────────────────────────────────────┐
│  User: Peter Buchegger (peterb)                          │
│  Status: aktiv                                           │
│  Gruppen: [admin]  [techniker]   [Gruppen bearbeiten]    │
│  ─────────────────────────────────────────────────       │
│  App-Zugriffe                                            │
│  ┌────────────────────────────────────────────────────┐ │
│  │ InvenTree     ● aktiv    seit 2026-04-12 [Entziehen]│ │
│  │ Grafana       ● aktiv    seit 2026-04-12 [Entziehen]│ │
│  │ Nextcloud     ○ keiner             [Gewähren]       │ │
│  │ Vereinswiki   ○ keiner             [Gewähren]       │ │
│  └────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

- Bootstrap-Card mit HTMX-getriggerten Toggle-Buttons (POST `/sso/grants/toggle/`).
- Jeder Toggle erzeugt `SsoAuditLog`-Eintrag.
- Sichtbar **nur für Admins**. Operator/Member sehen den Block nicht.

### 4.3 SSO-Übersicht

Neuer Sidebar-Eintrag „SSO" für Admins → `/sso/`:

```
SSO — Registered Apps
┌─────────────────────────────────────────────────────────┐
│ Application        Active Grants    Last Used          │
├─────────────────────────────────────────────────────────┤
│ InvenTree          12               2026-05-17         │
│ Grafana             8               2026-05-18         │
│ Nextcloud           0               nie                │
│ Vereinswiki         3               2026-05-10         │
└─────────────────────────────────────────────────────────┘
       [+ Neue App registrieren →]  (Link zu /admin/sso/application/add/)
```

Per Klick auf eine App → App-Detail mit Liste „Wer hat Zugriff?" + Grant/Revoke-Buttons (gleiche Toggle-API wie 4.2).

### 4.4 Audit-Log

Eigenes Modell `SsoAuditLog` — `StationAuditLog` ist per-Station, SSO-Events sind system-weit.

```python
class SsoAuditLog(models.Model):
    class EventType(TextChoices):
        APP_REGISTERED = "app_registered"
        APP_DELETED = "app_deleted"
        GRANT_GIVEN = "grant_given"
        GRANT_REVOKED = "grant_revoked"
        LOGIN_SUCCESS = "login_success"
        LOGIN_DENIED_NO_GRANT = "login_denied_no_grant"
        LOGIN_DENIED_INACTIVE = "login_denied_inactive"
        TOKEN_REVOKED = "token_revoked"

    event_type = CharField(...)
    actor = FK(User, null=True)        # wer ausgelöst
    target_user = FK(User, null=True)  # auf wen gerichtet
    application = FK("oauth2_provider.Application", null=True)
    message = TextField()
    ip_address = GenericIPAddressField(null=True)
    created_at = DateTimeField(auto_now_add=True)
```

Eingebunden in die bestehende `apps/audit/`-Übersicht: globales Audit-Log filtert nach `category=station|sso`, bestehender Tab bleibt rückwärtskompatibel.

---

## 5. Security

### 5.1 RSA-Keypair für ID-Token-Signatur

- RSA-2048 (RS256). Privatekey nur am Server, Publickey über JWKS.
- Bootstrap via `manage.py setup_oidc_keys` (idempotent, schreibt nach `/app/oidc_keys/private.pem`).
- Im Container: Docker-Volume `oidc_keys` gemounted unter `/app/oidc_keys` (persistent über Deploys — sonst invalidieren alle aktiven Tokens bei jedem Restart).
- Settings: `OAUTH2_PROVIDER["OIDC_RSA_PRIVATE_KEY"]` liest den Pfad ein.
- **Key-Rotation: nicht in V1.** Falls nötig: DOT unterstützt `OIDC_RSA_PRIVATE_KEYS_INACTIVE` für graceful Rotation.

### 5.2 Token-Storage

- DOT speichert Access/Refresh-Tokens als opake High-Entropy-Strings (32+ bytes Random) in der DB — wie Session-IDs. **Nicht** gehasht. Sicherheitseigenschaft kommt von DB-Schutz + TLS-Transport.
- DB-Dump-Leak wäre damit Token-Leak. Mitigations: HSTS+TLS überall (schon konfiguriert), keine DB-Dumps unverschlüsselt rumliegen lassen, Token-Lifetimes kurz halten (siehe 3.2).
- Authorization-Codes werden nach Einlösung sofort gelöscht.
- Optional in V2: Token-Hashing via DOT's `OAUTH2_HASH_ACCESS_TOKEN` (verfügbar in neueren DOT-Versionen, in Plan-Phase prüfen).

### 5.3 Brute-Force / Rate-Limiting

- Login geht über bestehende `accounts:login`-View → `django-axes` greift automatisch (5 Versuche/h, schon konfiguriert).
- `/sso/token/` braucht eigenes Rate-Limit: DRF `ScopedRateThrottle` mit `"token_exchange": "30/min"`.

### 5.4 Redirect-URI-Validation

- DOT macht **Exact-Match** gegen registrierte URIs (kein Wildcard, kein Suffix-Match).
- Bei `/sso/logout/` ebenso `post_logout_redirect_uri` nur gegen registrierte URIs.

### 5.5 User-Deaktivierung / Grant-Revoke kaskadiert auf Tokens

- post_save-Signal auf `User`: wenn `is_active` False wird → alle `AccessToken`/`RefreshToken` dieses Users revokieren.
- post_save-Signal auf `AppGrant`: wenn `revoked_at` gesetzt wird → Tokens für die betroffene Application revokieren.

→ Verhindert, dass deaktivierte User oder entzogene Grants bis zur natürlichen Token-Ablauf weiter funktionieren.

### 5.6 Logging-Sicherheit

- Kein `client_secret`, kein Token-Klartext, kein Auth-Code in irgendein Log.
- DOT macht das per Default richtig; eigene Hooks (`oidc_claims.py`, Audit-Log) müssen darauf achten.

### 5.7 CSP

Aktuelle CSP-Konfig in `config/settings/base.py` bleibt unverändert — RPs rufen den station-manager auf, nicht andersrum.

---

## 6. Testing

Strikte TDD via `superpowers:test-driven-development` Skill (in der Plan-Phase aktiv).

### 6.1 Unit-Tests

`tests/test_sso_models.py`, `test_sso_claims.py`, `test_sso_permissions.py`:
- `AppGrant` Uniqueness-Constraint (aktiv vs. revoked)
- `oidc_claims.add_claims()` Claim-Struktur für admin/operator/member + multi-group
- `user_can_access()` Matrix:
  - active + grant → True
  - inactive + grant → False
  - active + no_grant → False
  - active + revoked_grant → False
- `is_admin` / `is_operator` / `is_staff_member` Properties nach Group-Refactor

### 6.2 Integration-Tests

`tests/test_sso_flow.py` — Stern der Show, `authlib` als Test-Client:
- Vollständiger Auth-Code-Flow mit PKCE end-to-end: authorize → consent → token → userinfo → logout.
- Negative Pfade:
  - Missing AppGrant → access_denied
  - Falscher Code-Verifier → invalid_grant
  - Expired Code → invalid_grant
  - Replay des Codes → invalid_grant
  - Falsche redirect_uri → 400
- Refresh-Token-Flow: Token holen → Access expirieren lassen → Refresh → neuer Access-Token.
- Logout: Tokens werden DB-seitig revoked.
- User-Deaktivierung während aktiver Session: Signal revoked Tokens.
- Grant-Revoke während aktiver Session: Signal revoked Tokens nur für diese App.

### 6.3 Migrations-Tests

`tests/test_role_to_groups_migration.py`:
- Test-DB mit existierenden Users mit allen drei `role`-Werten.
- Migration laufen lassen → jeder User ist Mitglied genau einer Group entsprechend dem ursprünglichen `role`.
- `user.is_admin` etc. geben weiterhin korrekte Werte zurück.

### 6.4 E2E gegen echten RP (nice-to-have, nicht V1-Blocker)

- InvenTree-Container im `docker-compose-test`, Login durchklicken, UserInfo lesen.

---

## 7. Migration & Deployment

### 7.1 Schritt-für-Schritt-Reihenfolge

Jeder Schritt für sich ein deploybarer Commit:

1. **`apps/accounts` data migration `0XXX_role_to_groups`** — legt die 3 Groups idempotent an, weist jeden User seiner Group zu. Field `role` bleibt vorerst stehen.
2. **Code-Refactor:** alle `user.role == "admin"`-Sites auf `user.is_admin` umstellen. `User.is_admin` etc. werden auf cached_property von Groups umgestellt. Field `role` wird nicht mehr gelesen.
3. **Schema-Migration `0XXX_drop_role_field`** im nächsten Release.

Drei Commits = drei deploybare States, jeder funktionsfähig. Falls Rollback nötig zwischen Schritt 2 und 3 bleibt das Schema intakt.

### 7.2 RSA-Key-Bootstrap

- Deploy-Workflow: vor erstem Start `docker compose run --rm web python manage.py setup_oidc_keys`.
- Volume persistiert den Key über Container-Recreate.
- README/CONTRIBUTING dokumentiert den Step.

### 7.3 Bestehende Sessions / Auth

- Existierende User-Sessions am station-manager bleiben gültig (cookie-basiert, nicht betroffen).
- Station-Agent Ed25519-Auth: völlig separater Codepfad, **null Auswirkung**.

### 7.4 Deployment-Diff

**`docker-compose.yml` + `deploy/docker-compose.prod.yml`:**
- Neues benanntes Volume `oidc_keys` (gemounted in `web`-Service unter `/app/oidc_keys`).
- Worker-Container (`background-worker`, `station-monitor`, `alert-monitor`) brauchen den Key **nicht** — nur `web`-Container.
- Env-Variable `OIDC_RSA_KEY_PATH=/app/oidc_keys/private.pem` (Default).

**Nginx (`deploy/nginx.conf`):**
- `/sso/` Pfad muss durchgeschleift werden — vermutlich abgedeckt durch das catch-all `proxy_pass http://web:8000`. Beim Implementieren verifizieren.

### 7.5 Erste InvenTree-Integration (Ops-Aufgabe nach Merge, nicht Teil des Spec)

1. station-manager-Admin: neue `Application` anlegen — `client_id=inventree-prod`, `redirect_uris=https://parts.oe5xrx.org/accounts/oidc/login/callback/`, `client_secret` notieren.
2. AppGrants verteilen.
3. In InvenTree: `INVENTREE_OIDC_*` Settings setzen mit Discovery-URL + Client-Credentials.
4. Smoke-Test: User loggt sich ein.

---

## 8. Was bewusst NICHT drin ist (YAGNI)

- **2FA / MFA** — eigenes Spec. Heute hat station-manager auch kein 2FA, das ändert sich mit dem SSO-PR nicht.
- **Front-Channel Single-Logout** (alle Apps gleichzeitig austüten via iframes/postMessage) — V2-Kandidat.
- **Per-App Token-Lifetime-Overrides** — heute global 1h/14d, später per-Application konfigurierbar wenn Bedarf da ist.
- **SAML-Support** — Pfad nicht zugemacht (zweite Library zusätzlich zu DOT möglich), aber V1 ist OIDC only.
- **OIDC-Client-Side im station-manager** (= station-manager loggt sich gegen externen IdP ein, z.B. Google) — andere Richtung, kein Anwendungsfall.
- **Key-Rotation-Automatik** — manueller Prozess in V1, kann via DOT-Mechanismus später automatisiert werden.
- **Per-App-Rollen-Matrix** (User X = admin in InvenTree, member in Grafana) — bewusst nicht. RP-App entscheidet selbst, wie sie Vereins-Group auf eigene Rechte mappt ("B-light").

---

## 9. Offene Fragen für die Plan-Phase

- Exakte Form des `sub`-Claims: User-PK als String (heute Vorschlag) vs. UUID-Migration auf User? Default: PK-as-String, billig, keine Schema-Änderung.
- Initial-Bootstrap der drei Groups: über `create_default_alert_rules`-ähnliches Management-Command, oder direkt in der Data-Migration? Default: in der Data-Migration, sonst doppelte Wahrheit.
- `SsoAuditLog` neben `StationAuditLog` oder generisches `SystemAuditLog` mit Discriminator-Field? Default: getrennt halten — Migrationen sind billig wenn man später konsolidieren will.

Diese drei sind Implementierungs-Details, nicht Spec-Blocker — die Plan-Phase entscheidet konkret.

---

## 10. Roll-Up Checkliste

Was nach Merge dieses Specs implementiert ist:

- [ ] station-manager kann als OIDC-Provider auf `/sso/*` für andere Apps dienen.
- [ ] User-Verwaltung zentral; pro App ein Häkchen "darf rein".
- [ ] User-Gruppen sind via Django-`auth.Group` flexibel erweiterbar.
- [ ] Vereins-Gruppen wandern als `groups`-Claim in jedes ID-Token.
- [ ] Audit-Log für alle SSO-relevanten Events.
- [ ] Three-step-Migration ohne Code-Breakage zwischen den Releases.
- [ ] Bestehender Login-Flow am station-manager bleibt unverändert.
- [ ] Station-Agent Ed25519-Auth bleibt unverändert.
- [ ] Test-Coverage: Unit + Integration + Migrations + Auth-Code-Flow E2E.

Erste echte Verifikation: InvenTree-Integration nach Merge.
