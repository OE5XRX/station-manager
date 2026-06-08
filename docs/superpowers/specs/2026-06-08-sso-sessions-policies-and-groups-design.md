# SSO — Sessions, Hausordnung pro App und Gruppen-Weitergabe — Design

**Status:** Draft, brainstormed 2026-06-08.
**Ziel:** Den bestehenden SSO/OIDC-Provider des station-manager um drei Bereiche erweitern, die der erste Spec (`2026-05-18-sso-oidc-provider-design.md`) bewusst ausgeklammert hat:

1. **Hausordnung pro App** — Auto-Freischaltung von Apps wie InvenTree, sodass nicht für jeden User einzeln ein Häkchen gesetzt werden muss.
2. **Session-Tracking + Admin-Revoke** — pro Login eine sichtbare Session mit Standort/Browser/Letzte-Aktivität; einzelne Sessions können gezielt beendet werden.
3. **Gruppen-Weitergabe an RP-Apps** — was heute eine leere Liste im OIDC-Token ist, wird befüllt aus den existierenden strukturierten Daten (membership_level + Station/Region-Assignments) plus optionalen freien Tags.

Der Spec ist für Entwickler geschrieben, die mit dem Codebase vertraut sind, aber nicht zwingend OIDC-Spezialisten. Begriffe werden bei der ersten Verwendung erklärt.

---

## 1. Kontext und Begriffe

Der station-manager ist seit PR #45 ein **OIDC-Identity-Provider** (kurz: IdP). Andere Vereins-Apps (InvenTree für Lager, Grafana für Messdaten, …) sind die **Relying-Parties** (RPs) — sie haben kein eigenes Login, sondern delegieren das an den station-manager.

Datenstand heute:

- Jede RP-App ist im station-manager als `oauth2_provider.Application` registriert (Django OAuth Toolkit, kurz DOT).
- Pro (User × Application) braucht es einen aktiven `AppGrant`-Eintrag, sonst wird kein Token ausgegeben (Authorize-Endpoint antwortet mit `access_denied`).
- Beim Login bekommt die RP-App vom station-manager ein **ID-Token** (signiertes JSON-Web-Token mit User-Daten) plus ein **Access-Token** (für API-Calls) plus ein **Refresh-Token** (zum Verlängern des Access-Tokens für 14 Tage).
- Wird ein User deaktiviert oder ein AppGrant zurückgezogen, kaskadieren Django-Signals: alle laufenden Tokens dieses Users (bzw. dieser User-App-Kombi) werden ungültig gesetzt.

Was aktuell **nicht** geht und dieser Spec adressiert:

- Apps, die jedem Vereinsmitglied offenstehen sollen (typisches Beispiel: InvenTree-Lagerverwaltung), brauchen heute trotzdem für jeden User einen manuellen AppGrant. Operative Friction.
- Es gibt keine Sicht darauf, wer wann von wo eingeloggt ist. Ein User kann fünf gültige Sessions in fünf Browsern haben, der Admin sieht keine davon.
- Die einzige Revoke-Granularität ist heute "alle Tokens dieses Users" oder "alle Tokens dieses Users für diese App". Eine einzelne verlorene Session (Handy weg, Public-PC-Login vergessen) kann man nicht gezielt beenden.
- Der `groups`-Claim im OIDC-Token ist heute immer leer: PR #55 hat die legacy Django-Gruppen `admin`/`operator`/`member` gelöscht (Migration `accounts/0007_drop_legacy_role_groups`), und in `oidc_claims.py` steht weiterhin `claims["groups"] = list(user.groups.values_list("name", flat=True))` — was jetzt eine leere Liste produziert. RPs haben damit keine Möglichkeit, Vereins-Rollen auf interne Berechtigungen zu mappen.

---

## 2. Architektur — Überblick

Zwei neue Modelle in `apps/sso`:

- **`ApplicationPolicy`** — 1:1 zu `oauth2_provider.Application`. Hält die "Hausordnung" (wer darf rein).
- **`TokenSession`** — 1:1 zu jeder ausgegebenen `RefreshToken`-Issuance. Speichert IP, User-Agent, Standort, Zeitstempel, Revoke-Status.

Zwei erweiterte Funktionen in `apps/sso`:

- **`user_can_access(user, application)`** — konsultiert jetzt zuerst die Hausordnung; AppGrant-Check bleibt der Fallback für Policy `GRANT_REQUIRED`.
- **`oidc_claims.add_claims`** — produziert eine synthetische `groups`-Liste aus membership_level, StationAssignment, RegionAssignment und Django auth.Group-Mitgliedschaften (mit `tag:`-Präfix).

Ein Validator-Hook neu:

- **`SsoOAuth2Validator.save_bearer_token`** override — erzeugt bei Token-Ausgabe (initial und bei Refresh-Rotation) eine `TokenSession`-Row, bumped `last_seen_at` der Vorgänger-Session.

Ein neuer externer Service:

- **GeoIP-Lookup** via [db-ip.com Free](https://db-ip.com/db/lite.php) (kein API-Token, mmdb-Format, `geoip2` Python-Library). Lookup beim Token-Issue, Land + Stadt persistiert in `TokenSession`.

UI-Erweiterungen auf bestehenden Seiten plus eine neue:

- User-Detail kriegt eine "Active Sessions"-Card und eine "Tags"-Card.
- App-Detail kriegt einen Policy-Selector + eine "Group propagation"-Section.
- SSO-Dashboard kriegt eine KPI-Tile "Active sessions" und eine "Policy"-Spalte.
- Neue Seite `/sso-admin/tags/` für Tag-Management.

Keine bestehende Tabelle ändert sich. Keine DOT-internen Modelle werden geswappt.

---

## 3. Hausordnung pro App — `ApplicationPolicy`

### 3.1 Modell

```python
# apps/sso/models.py

class ApplicationPolicy(models.Model):
    """Per-App access policy. 1:1 zu DOT's Application.

    Wenn keine Row existiert -> Policy ist implizit GRANT_REQUIRED, was
    dem bisherigen Verhalten entspricht. Damit ist die Migration null-
    -kosten: bestehende Apps verhalten sich exakt wie vorher, bis der
    Admin explizit eine Policy setzt.
    """

    class AccessPolicy(models.TextChoices):
        GRANT_REQUIRED   = "grant_required",   _("Grant required (default)")
        OPEN_TO_ALL      = "open_to_all",      _("Open to all (incl. applicants)")
        OPEN_TO_MEMBERS  = "open_to_members",  _("Open to members and above")
        OPEN_TO_INTERNAL = "open_to_internal", _("Open to staff and admins")
        OPEN_TO_ADMINS   = "open_to_admins",   _("Open to admins only")

    application = models.OneToOneField(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="sso_policy",
    )
    access_policy = models.CharField(
        max_length=32,
        choices=AccessPolicy.choices,
        default=AccessPolicy.GRANT_REQUIRED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    modified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="modified_app_policies",
    )

    class Meta:
        verbose_name = _("application policy")
        verbose_name_plural = _("application policies")

    def __str__(self):
        return f"{self.application.name} -> {self.get_access_policy_display()}"
```

### 3.2 Gate-Logik in `user_can_access`

`apps/sso/permissions.py` wird so umgebaut:

```python
def user_can_access(user, application) -> bool:
    """Return True iff user is active AND policy/grant allows access."""
    if not getattr(user, "is_active", False):
        return False  # rote Linie: inaktiv = niemals rein, unabhaengig von Policy

    from .models import AppGrant, ApplicationPolicy

    policy = ApplicationPolicy.AccessPolicy.GRANT_REQUIRED
    pol_obj = getattr(application, "sso_policy", None)
    if pol_obj is not None:
        policy = pol_obj.access_policy

    if policy == ApplicationPolicy.AccessPolicy.OPEN_TO_ALL:
        return True
    if policy == ApplicationPolicy.AccessPolicy.OPEN_TO_MEMBERS:
        return user.membership_level != user.MembershipLevel.APPLICANT
    if policy == ApplicationPolicy.AccessPolicy.OPEN_TO_INTERNAL:
        return user.is_internal
    if policy == ApplicationPolicy.AccessPolicy.OPEN_TO_ADMINS:
        return user.is_admin

    # GRANT_REQUIRED -- bisheriges Verhalten
    return AppGrant.objects.filter(
        user=user, application=application, revoked_at__isnull=True,
    ).exists()
```

### 3.3 Policy-Wechsel und laufende Sessions

Ein Policy-Wechsel **invalidiert keine existierenden Tokens automatisch**. Begründung:

- Verschärfung (z.B. `OPEN_TO_ALL` -> `OPEN_TO_INTERNAL`) ist ein bewusster Vorgang; der Admin will potenziell nicht, dass alle Vereinsmitglieder schlagartig ausgesperrt werden, sondern dass *neue* Tokens nur noch an Staff gehen. Existierende Sessions laufen 14 Tage aus (Refresh-Token-Lifetime) und werden beim nächsten Refresh-Versuch geprüft.
- Wenn ein sofortiger Kick gewünscht ist, gibt es zwei explizite Wege: (a) Massen-Revoke pro App im Admin-UI als separate Aktion (zukünftige Erweiterung, nicht V1), oder (b) jeden betroffenen User einzeln im Session-UI abwürgen.

Beim Refresh-Versuch eines abgelaufenen Access-Tokens läuft der Code wieder durch `user_can_access` (über den `validate_refresh_token`-Pfad in DOT) und greift die neue Policy. Damit ist die "natürliche" Wirkung des Policy-Wechsels: spätestens nach Access-Token-Lebenszeit (1 h) merkt es jeder.

### 3.4 Audit

Neuer Event-Typ `APP_POLICY_CHANGED` in `SsoAuditLog.EventType`. Nachricht beinhaltet: alte Policy, neue Policy, Anzahl betroffener aktiver Sessions zum Zeitpunkt des Wechsels (Snapshot für Forensik).

---

## 4. Session-Tracking — `TokenSession`

### 4.1 Modell

```python
# apps/sso/models.py

class TokenSession(models.Model):
    """1:1 zu jeder RefreshToken-Issuance (inkl. Rotations-Chain).

    Bei jedem Token-Issue (Authorization-Code-Exchange ODER Refresh-Rotation)
    wird eine neue Row erzeugt. 'parent' zeigt auf die Vorgänger-Session
    bei Refresh, so dass die "Session-Reihe" über die DOT-interne
    Token-Rotation hinweg rekonstruierbar bleibt.

    Eine TokenSession gilt als "aktiv", wenn:
      - revoked_at is None
      - refresh_token existiert (nicht NULL durch FK-Cascade)
      - refresh_token.revoked is None
      - refresh_token.access_token.expires > now() ODER ein noch
        nicht ausgetauschtes Refresh-Token existiert
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="token_sessions",
    )
    application = models.ForeignKey(
        "oauth2_provider.Application",
        on_delete=models.CASCADE,
        related_name="token_sessions",
    )
    refresh_token = models.OneToOneField(
        "oauth2_provider.RefreshToken",
        on_delete=models.CASCADE,
        related_name="sso_session",
        null=True, blank=True,
    )
    parent = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="children",
    )

    # Connection metadata at the moment of token issuance.
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    country_code = models.CharField(max_length=2, blank=True)  # ISO-3166-1 alpha-2
    city = models.CharField(max_length=100, blank=True)

    issued_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now_add=True)  # bumped bei Refresh

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_sessions",
    )

    class RevokeReason(models.TextChoices):
        ADMIN_REVOKE = "admin_revoke", _("Admin revoke")
        USER_LOGOUT = "user_logout", _("User logout")
        USER_DEACTIVATED = "user_deactivated", _("User deactivated")
        GRANT_REVOKED = "grant_revoked", _("Grant revoked")
        ROTATED = "rotated", _("Rotated (refresh)")

    revoke_reason = models.CharField(
        max_length=32, choices=RevokeReason.choices, blank=True,
    )

    class Meta:
        verbose_name = _("token session")
        verbose_name_plural = _("token sessions")
        ordering = ("-issued_at",)
        indexes = [
            models.Index(fields=["user", "-issued_at"]),
            models.Index(fields=["application", "-issued_at"]),
            models.Index(fields=["revoked_at"]),
        ]

    @property
    def is_active(self) -> bool:
        """Lebendige Session: nicht revoked, RefreshToken intakt, AccessToken
        oder Refresh-Lifetime noch nicht abgelaufen."""
        if self.revoked_at is not None:
            return False
        rt = self.refresh_token
        if rt is None or rt.revoked is not None:
            return False
        # Conservative: Refresh-Lifetime ueber den Token-Default
        from django.utils import timezone
        from datetime import timedelta
        max_lifetime = timedelta(seconds=settings.OAUTH2_PROVIDER.get(
            "REFRESH_TOKEN_EXPIRE_SECONDS", 14 * 24 * 3600,
        ))
        return self.issued_at + max_lifetime > timezone.now()
```

### 4.2 Hook in `SsoOAuth2Validator.save_bearer_token`

DOT ruft `save_bearer_token(token, request, *args, **kwargs)` auf, nachdem Access+Refresh-Token in der DB persistiert wurden. `token` ist ein Dict mit `access_token`, `refresh_token`, `expires_in`, etc. `request` ist ein **oauthlib**-Request-Objekt (NICHT Django-HttpRequest), das die Verbindungs-Metadaten in `request.headers` und `request.client` hat.

```python
# apps/sso/permissions.py

class SsoOAuth2Validator(OAuth2Validator):
    # ... bestehender Code ...

    def save_bearer_token(self, token, request, *args, **kwargs):
        super().save_bearer_token(token, request, *args, **kwargs)
        try:
            self._record_token_session(token, request)
        except Exception:
            # Session-Tracking ist Observability, kein Security-Gate.
            # Ein DB-Fehler darf NIE die Token-Ausgabe brechen.
            logger.exception("TokenSession recording failed")

    def _record_token_session(self, token, request):
        from .models import TokenSession
        from .geoip import lookup_location  # siehe Section 6
        from oauth2_provider.models import RefreshToken

        refresh_value = token.get("refresh_token")
        if not refresh_value:
            return  # client_credentials-grant o.ae., kein Refresh -> kein Session-Tracking
        rt = RefreshToken.objects.filter(token=refresh_value).first()
        if rt is None:
            return

        # Parent finden: falls dies ein Refresh-Rotation ist, hat der oauthlib-
        # Request ein 'refresh_token' Attribut mit dem ALTEN Token-Wert (vor
        # Rotation). Wir suchen die zugehoerige TokenSession.
        parent_session = None
        old_refresh = getattr(request, "refresh_token_instance", None)
        if old_refresh is not None:
            parent_session = TokenSession.objects.filter(
                refresh_token=old_refresh,
            ).first()
            if parent_session is not None:
                from django.utils import timezone
                parent_session.last_seen_at = timezone.now()
                parent_session.revoked_at = timezone.now()
                parent_session.revoke_reason = TokenSession.RevokeReason.ROTATED
                parent_session.save(update_fields=[
                    "last_seen_at", "revoked_at", "revoke_reason",
                ])

        ip = self._extract_ip(request)
        ua = (request.headers or {}).get("User-Agent", "")[:512]
        country, city = lookup_location(ip)

        TokenSession.objects.create(
            user=rt.user,
            application=rt.application,
            refresh_token=rt,
            parent=parent_session,
            ip_address=ip,
            user_agent=ua,
            country_code=country or "",
            city=city or "",
        )

        # Audit: LOGIN_SUCCESS (das EventType existiert seit V1 im Enum,
        # wurde aber bisher nie emittiert).
        if parent_session is None:
            from .models import SsoAuditLog
            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.LOGIN_SUCCESS,
                target_user=rt.user,
                application=rt.application,
                message=f"Token issued. UA={ua[:80]} City={city or 'unknown'}",
                ip_address=ip,
            )

    @staticmethod
    def _extract_ip(oauthlib_request):
        headers = oauthlib_request.headers or {}
        xff = headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return headers.get("X-Real-IP")  # nginx-Standard-Header
```

**Wichtig — oauthlib vs. Django Request:** Das `request`-Objekt, das DOT an die Validator-Methode übergibt, ist NICHT der gewohnte Django-HttpRequest. Es ist ein oauthlib `Request`-Objekt, das Header in einem Dict trägt (das DOT aus dem Django-Request hineingespiegelt hat). Das bedeutet: kein `request.META`, kein `request.user` direkt. IPs müssen aus den X-Forwarded-For/X-Real-IP-Headern gelesen werden, die nginx vor station-manager setzt.

### 4.3 Cascade-Erweiterung in `signals.py`

Die bestehenden Signal-Handler in `apps/sso/signals.py` revokieren bei User-Deaktivierung und Grant-Revoke die DOT-Tokens. Sie müssen erweitert werden, um auch die zugehörigen `TokenSession`-Rows als revoked zu markieren:

```python
def _mark_sessions_revoked(user, *, application=None, reason):
    """Helper: setzt revoked_at auf allen aktiven TokenSessions des Users
    (optional gefiltert auf eine App). Wird aus den bestehenden Cascade-
    Handlern aufgerufen."""
    from .models import TokenSession
    qs = TokenSession.objects.filter(user=user, revoked_at__isnull=True)
    if application is not None:
        qs = qs.filter(application=application)
    from django.utils import timezone
    qs.update(revoked_at=timezone.now(), revoke_reason=reason)
```

Aufruf-Stellen:
- `_revoke_tokens_on_user_deactivation` -> `_mark_sessions_revoked(instance, reason=USER_DEACTIVATED)`
- `_revoke_tokens_for_user_and_app` -> `_mark_sessions_revoked(user, application=application, reason=GRANT_REVOKED)`

### 4.4 Admin-Revoke-Endpunkt

Neue View `SessionRevokeView` in `apps/sso/views.py`, gemounted unter `/sso-admin/sessions/<int:pk>/revoke/`:

```python
class SessionRevokeView(AdminOnlyMixin, View):
    """POST-only: einzelne TokenSession revoken. Idempotent."""

    def post(self, request, pk):
        session = get_object_or_404(TokenSession, pk=pk)
        if session.revoked_at is None:
            with transaction.atomic():
                # 1. Refresh-Token in DOT revoken (Cascade greift NICHT,
                #    weil das Signal nur auf Grant-Revoke und User-Deactivate
                #    feuert -- hier ist beides nicht der Fall).
                from oauth2_provider.models import AccessToken
                from django.utils import timezone
                from datetime import timedelta
                rt = session.refresh_token
                if rt is not None and rt.revoked is None:
                    rt.revoked = timezone.now()
                    rt.save(update_fields=["revoked"])
                    # Access-Tokens mit derselben Quelle ablaufen lassen.
                    AccessToken.objects.filter(
                        source_refresh_token=rt,
                    ).update(expires=timezone.now() - timedelta(seconds=1))

                # 2. Session-Row als revoked markieren.
                session.revoked_at = timezone.now()
                session.revoked_by = request.user
                session.revoke_reason = TokenSession.RevokeReason.ADMIN_REVOKE
                session.save(update_fields=[
                    "revoked_at", "revoked_by", "revoke_reason",
                ])

            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.SESSION_REVOKED,
                actor=request.user,
                target_user=session.user,
                application=session.application,
                message=(
                    f"Session {session.pk} revoked. "
                    f"Issued {session.issued_at.isoformat()} "
                    f"from {session.ip_address} ({session.city or 'unknown'})"
                ),
                ip_address=_client_ip(request),
            )

        # Antwort:
        #   - HTMX-Request: 200 mit gerendertem _sessions_card.html-Partial,
        #     das die jetzt-revoked Session ausgrau (oder rausnimmt, je
        #     nach Filter aktiv/alle).
        #   - Klassischer Browser-POST: 302 zurueck auf die Referer-URL
        #     (User-Detail oder App-Detail), Flash-Message "Session abgewuergt".
        if request.htmx:
            return render(request, "sso/_sessions_card.html", {
                "target_user": session.user,
                "sessions": _active_sessions_for(session.user),
            })
        return HttpResponseRedirect(request.META.get("HTTP_REFERER", reverse("sso:dashboard")))
```

`SESSION_REVOKED` wird zu `SsoAuditLog.EventType` hinzugefügt — separat von dem schon existierenden `TOKEN_REVOKED` (das die *kaskadierenden* Token-Revokes via Signal markiert), damit man "admin hat eine Session abgewuergt" von "User wurde deaktiviert, alle Tokens gefallen" unterscheiden kann.

### 4.5 Cleanup

Management-Command `prune_token_sessions` (idempotent):

```python
class Command(BaseCommand):
    help = "Loescht TokenSession-Rows, deren RefreshToken seit >30d expired oder revoked ist."

    def handle(self, *args, **opts):
        cutoff = timezone.now() - timedelta(days=30)
        qs = TokenSession.objects.filter(
            Q(refresh_token__isnull=True) |
            Q(refresh_token__revoked__lt=cutoff) |
            Q(revoked_at__lt=cutoff),
        )
        n = qs.count()
        qs.delete()
        self.stdout.write(f"Pruned {n} TokenSession rows.")
```

Soll als Cron einmal pro Tag laufen. Doku-Sache; kein Code im Container-Entrypoint. Tabelle wächst sonst monoton mit jeder Token-Rotation.

---

## 5. Gruppen-Synthese — befüllter `groups`-Claim

### 5.1 Quellen der synthetischen Liste

`apps/sso/oidc_claims.py` wird umgebaut, sodass der `groups`-Claim eine Liste aus vier Quellen ist:

1. **Membership-Level** (außer Applicant): `"member"`, `"staff"`, `"admin"`.
2. **Station-Assignments**: `"station:<station-slug>:<role>"` pro StationAssignment.
3. **Region-Assignments**: `"region:<region-slug>:<role>"` pro RegionAssignment.
4. **Django auth.Group-Mitgliedschaften**: `"tag:<group-name>"` pro Mitgliedschaft.

### 5.2 Code-Skizze

```python
# apps/sso/oidc_claims.py

def add_claims(claims, user, request):
    claims["preferred_username"] = user.username
    claims["email"] = user.email or ""
    claims["email_verified"] = bool(user.email)
    claims["name"] = user.get_full_name() or user.username
    claims["locale"] = getattr(user, "language", "en") or "en"
    claims["groups"] = _build_groups(user)
    return claims


def _build_groups(user):
    """Synthetische groups-Liste aus membership_level + topology + tags.

    Reihenfolge ist deterministisch fuer Test-Stabilitaet und damit RPs,
    die die Liste einmal lexikographisch sortieren, kein Diff sehen,
    wenn sich nur die Reihenfolge der Quellen aendert.
    """
    groups = []

    # 1. Membership-Level (alle vier Werte werden propagiert, inklusive
    #    Applicant. Use-Case: eine RP wie eine Trainings-Software kann
    #    Applicant-spezifische Inhalte anzeigen ("Einsteiger-Trainings"),
    #    Member-Inhalte verbergen, etc. Applicant ist die niedrigste
    #    Stufe, der String impliziert keine Permission-Eskalation in der
    #    RP -- die RP entscheidet selbst, was sie damit anstellt.)
    groups.append(user.membership_level)  # "applicant"/"member"/"staff"/"admin"

    # 2. Station-Assignments
    for assignment in user.station_assignments.select_related("station"):
        groups.append(f"station:{assignment.station.slug}:{assignment.role}")

    # 3. Region-Assignments
    for assignment in user.region_assignments.select_related("region"):
        groups.append(f"region:{assignment.region.slug}:{assignment.role}")

    # 4. Freie Django auth.Group-Tags
    for name in user.groups.values_list("name", flat=True):
        groups.append(f"tag:{name}")

    return sorted(set(groups))
```

### 5.3 Beispiel-Token

Für Peter (Vereins-Mitglied, StationAdmin von OE5XRX-1, Region-Manager Wien, in den Django-Groups `kontakt-team` und `buehne-techniker`):

```json
{
  "preferred_username": "peter",
  "email": "peter@oe5xrx.org",
  "name": "Peter Buchegger",
  "groups": [
    "member",
    "region:wien:manager",
    "station:oe5xrx-1:admin",
    "tag:buehne-techniker",
    "tag:kontakt-team"
  ]
}
```

Für eine Anwärterin (Applicant) Anna ohne Assignments oder Tags:

```json
{
  "preferred_username": "anna",
  "groups": ["applicant"]
}
```

Eine Trainings-RP-App kann darauf direkt mappen: `"applicant"` → Einsteiger-Modul, `"member"` → Fortgeschrittenen-Modul.

### 5.4 RP-Mapping-Konvention (Doku-Anhang)

Die Liste ist ein **Vertrag** — RP-Operatoren werden ihre internen Rollen darauf mappen. Eine kurze Doku-Seite im SSO-Admin-Bereich enthält Copy-Paste-Snippets für die häufigen RPs (InvenTree, Grafana, Nextcloud), z.B.:

```yaml
# InvenTree config snippet
SSO_GROUP_MAP:
  "admin":                "inventree.admin"
  "staff":                "inventree.staff"
  "member":               "inventree.member"
  "tag:buehne-techniker": "inventree.lager_editor"
```

Die Doku-Seite ist Teil des Specs-Repositories (nicht externe URL), damit sie versioniert mit dem Code wandert.

### 5.5 Scope-Gating

Der `groups`-Claim wird vom Token nur ausgegeben, wenn die RP-App den Scope `groups` angefordert hat. Das ist bereits in der existierenden `SSO_CLAIM_SCOPE`-Map korrekt verdrahtet (`SSO_CLAIM_SCOPE["groups"] = "groups"`); das ändern wir nicht.

### 5.6 Tag-Management-UI

Neue Seite `/sso-admin/tags/` mit:

- Liste aller Django-Groups (= Tags) im System: Name, Mitgliederzahl, Letzte-Mitgliedschaftsänderung.
- "+ Neuer Tag" Inline-Form (Name, optionale Beschreibung).
- Klick auf Tag öffnet Detail-Seite: Mitgliederliste mit Toggle pro User.

User-Detail-Seite (in `apps/accounts/templates/accounts/`) bekommt eine "Tags"-Card mit Toggle-Buttons; Mechanik analog zur AppGrant-Toggle-Card.

Neuer Audit-Event-Typ `GROUP_MEMBERSHIP_CHANGED` mit Message-Format `added: <user> -> <tag>` bzw. `removed: <user> -> <tag>`.

---

## 6. GeoIP-Integration — db-ip.com Free

### 6.1 Provider-Entscheidung

**Wahl: db-ip.com Free** (kein API-Token nötig).

Vergleich:

| | db-ip.com Free | MaxMind GeoLite2 City | IP2Location LITE |
|---|---|---|---|
| API-Token nötig | nein | ja (kostenlos) | nein |
| Format | mmdb (MaxMind-kompatibel) | mmdb | proprietär |
| Update-Frequenz | monatlich | wöchentlich | monatlich |
| Python-Library | `geoip2` (Standard) | `geoip2` | `ip2location` |
| DB-Größe | ~150 MB | ~70 MB | ~50 MB |

Für eine reine "wo war die Person eingeloggt"-Anzeige ist die monatliche Update-Frequenz mehr als ausreichend (die IPs von ISP-Pools ändern sich nicht im Wochenrhythmus). Der Wegfall des License-Key (kein Account, kein Env-Var-Management, keine Rotation, kein "wer hat den verlängert"-Risiko) ist mehr wert als der wöchentliche Update von MaxMind. Beide nutzen das gleiche mmdb-Format -- ein späterer Wechsel auf MaxMind ist ein 1-Zeilen-Konfig-Swap.

### 6.2 Modul `apps/sso/geoip.py`

```python
"""Thin wrapper around geoip2.database.Reader.

Singleton-Reader (geoip2 ist threadsafe). Wenn DB-Datei fehlt oder
Lookup scheitert -> (None, None), Caller speichert leere Strings.

Die DB-Datei lebt unter settings.GEOIP_DB_PATH (Default
/app/geoip_db/dbip-city-lite.mmdb), persistiert in einem Docker-
Volume.
"""

import logging
import threading
from pathlib import Path

import geoip2.database
import geoip2.errors
from django.conf import settings

logger = logging.getLogger(__name__)

_reader = None
_reader_lock = threading.Lock()
_reader_load_failed = False


def _get_reader():
    global _reader, _reader_load_failed
    if _reader is not None:
        return _reader
    if _reader_load_failed:
        return None  # nicht in jedem Request den File-Stat triggern
    with _reader_lock:
        if _reader is not None:
            return _reader
        path = Path(settings.GEOIP_DB_PATH)
        if not path.exists():
            logger.warning("GeoIP DB nicht gefunden: %s -- Lookup deaktiviert", path)
            _reader_load_failed = True
            return None
        try:
            _reader = geoip2.database.Reader(str(path))
        except Exception:
            logger.exception("GeoIP DB-Reader konnte nicht initialisiert werden")
            _reader_load_failed = True
            return None
    return _reader


def lookup_location(ip: str | None) -> tuple[str | None, str | None]:
    """Resolve (country_code, city) fuer eine IP. None/None bei Fehler.

    Niemals raise; ein kaputter GeoIP-Lookup darf Token-Issuance nicht
    blockieren.
    """
    if not ip:
        return None, None
    reader = _get_reader()
    if reader is None:
        return None, None
    try:
        resp = reader.city(ip)
    except geoip2.errors.AddressNotFoundError:
        return None, None
    except Exception:
        logger.exception("GeoIP-Lookup fehlgeschlagen fuer %s", ip)
        return None, None
    return resp.country.iso_code, resp.city.name
```

### 6.3 Management-Command `update_geoip_db`

```python
"""Download + atomic replace of the db-ip.com City Lite DB.

URL-Schema: https://download.db-ip.com/free/dbip-city-lite-YYYY-MM.mmdb.gz

Release-Kadenz von db-ip.com: monatlich, aber NICHT garantiert am Monats-
ersten -- typischerweise innerhalb der ersten Wochen-Tage des Monats,
gelegentlich später. Deshalb:
  1. Cron lauft TAEGLICH (nicht monatlich), siehe Section 14.2.
  2. Bei 404 auf die aktuelle Monatsdatei -> Fallback auf Vormonat.
     Damit kann der erste Deploy auch dann frische Daten ziehen, wenn
     der laufende Monat noch nicht publiziert wurde (Worst-Case 1 Tag
     Lag statt 1-3 Monaten bei strikt-1.-des-Monats-Cron).

Das Kommando ist idempotent: identische URL liefert identischen Inhalt,
der atomare Replace ist ein No-Op (modulo File-Stat-Timestamp). Daily-
Cron erzeugt damit keinen unnötigen Restart-Druck.
"""

from datetime import date
from pathlib import Path
import gzip
import shutil
import tempfile
import urllib.error
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand


DBIP_URL_TEMPLATE = "https://download.db-ip.com/free/dbip-city-lite-{year_month}.mmdb.gz"


def _previous_month(today: date) -> date:
    """Erster Tag des Vormonats. Stdlib statt python-dateutil:
    dateutil ist nur transitiv via boto3 da -- nicht als direkte
    Dependency deklariert, also benutzen wir's nicht."""
    if today.month == 1:
        return today.replace(year=today.year - 1, month=12, day=1)
    return today.replace(month=today.month - 1, day=1)


class Command(BaseCommand):
    help = "Lade die db-ip.com City Lite DB herunter und ersetze die lokale Kopie atomar."

    def handle(self, *args, **opts):
        target = Path(settings.GEOIP_DB_PATH)
        target.parent.mkdir(parents=True, exist_ok=True)

        today = date.today()
        candidates = [
            today.strftime("%Y-%m"),
            _previous_month(today).strftime("%Y-%m"),
        ]

        downloaded_from = None
        for year_month in candidates:
            url = DBIP_URL_TEMPLATE.format(year_month=year_month)
            try:
                self._download(url, target)
                downloaded_from = year_month
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self.stdout.write(self.style.WARNING(
                        f"{year_month} not yet published (404), trying previous"
                    ))
                    continue
                raise

        if downloaded_from is None:
            # Beide Kandidaten 404 -- ungewoehnlich (db-ip waere zwei
            # Monate ohne Release). Exit non-zero damit der Workflow
            # rot wird und der Operator informiert ist; bestehende DB
            # bleibt unangetastet, Lookups funktionieren weiter mit
            # alten Daten.
            raise SystemExit(
                f"Both {candidates[0]} and {candidates[1]} return 404 -- "
                f"db-ip.com release schedule changed? Manual check needed."
            )

        self.stdout.write(self.style.SUCCESS(
            f"Updated {target} from db-ip.com {downloaded_from}"
        ))

        # GeoIP-Reader-Singleton zuruecksetzen (siehe Modul-Docstring von
        # apps/sso/geoip.py): nur der Process, in dem das Command lief,
        # sieht das Reset; andere worker halten ihren alten Reader bis
        # Restart. Bei daily-cron + 14d-token-lifetime ist das tolerierbar.
        from apps.sso import geoip
        geoip._reader = None
        geoip._reader_load_failed = False

    def _download(self, url: str, target: Path) -> None:
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent) as tmp:
            tmp_path = Path(tmp.name)
        try:
            self.stdout.write(f"Download {url} ...")
            with urllib.request.urlopen(url) as resp, gzip.GzipFile(fileobj=resp) as gz:
                with tmp_path.open("wb") as out:
                    shutil.copyfileobj(gz, out)
            # Atomarer Tausch: os.rename auf POSIX ist atomar im selben FS.
            tmp_path.replace(target)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
```

Soll als Cron einmal pro Tag laufen (siehe Section 14.2). Bei jeder Ausführung wird zuerst die Datei für den laufenden Monat versucht; wenn db-ip diese Monatsdatei noch nicht hochgeladen hat (Release-Lag), wird die Vormonats-Datei gezogen. Resultierender max. Lag zwischen db-ip-Publikation und unserer Übernahme: ~1 Tag.

### 6.4 Settings-Erweiterung

```python
# config/settings/base.py
GEOIP_DB_PATH = os.environ.get(
    "GEOIP_DB_PATH",
    str(BASE_DIR / "geoip_db" / "dbip-city-lite.mmdb"),
)
```

```yaml
# docker-compose.yml / deploy/docker-compose.prod.yml
services:
  web:
    volumes:
      - geoip_db:/app/geoip_db
volumes:
  geoip_db:
```

### 6.5 Erstbefüllung

Initial-Deploy-Schritt (analog zu `setup_oidc_keys` aus PR #45):

```bash
docker compose run --rm web python manage.py update_geoip_db
```

In README + CONTRIBUTING dokumentiert.

---

## 7. UI-Erweiterungen

### 7.1 SSO-Dashboard (`/sso-admin/`)

Erweitert um:

- KPI-Tile "Active sessions: N (across M apps)" neben dem bestehenden "Active grants" Tile.
- App-Tabelle bekommt neue Spalte "Policy" mit Badge:
  - Grau "Grant required" (Default)
  - Grün "Open to all" / "Open to members" / "Open to internal" / "Open to admins" (je nach Policy)
- App-Tabelle bekommt neue Spalte "Sessions" (Count aktiver TokenSessions).

### 7.2 App-Detail (`/sso-admin/applications/<pk>/`)

Erweitert um drei neue Sektionen:

1. **Policy-Selector oben:**
   ```
   Access policy: [Grant required v]   [Save]
   ```
   Save -> POST -> Audit `APP_POLICY_CHANGED`, Page-Reload mit Flash-Message inkl. "this affects N active sessions".
2. **Group propagation:**
   - Liste der Groups, die heute für **irgendeinen** User im System propagiert würden (= alle in `_build_groups` möglichen Werte; abgeleitet aus aktuellen membership_level/Station/Region/Tag-Daten).
   - Link auf RP-Mapping-Doku.
3. **Recent Sessions auf dieser App** (letzte 50, Tabelle mit User, Standort, Browser, issued_at, last_seen_at, Status, Revoke-Button).

### 7.3 User-Detail (in `apps/accounts/templates/accounts/`)

Zwei neue Cards (zusätzlich zu den bestehenden Cards aus PR #56):

1. **Active Sessions:**
   ```
   ┌─ Active Sessions ──────────────────────────────────────────────────────┐
   │ App        Issued        Last seen   Standort     Gerät                │
   ├────────────────────────────────────────────────────────────────────────┤
   │ InvenTree  03 Jun 14:21  08 Jun 09:14 AT Linz     FF/Linux  [Abwürgen] │
   │ Grafana    07 Jun 22:03  07 Jun 22:03 AT Wien     Safari/iOS [Abwürgen]│
   └────────────────────────────────────────────────────────────────────────┘
   ```
   User-Agent wird mit einer kleinen heuristischen Funktion zu "FF/Linux", "Safari/iOS" etc. zusammengefasst (kein UA-Parser-Library — wir wollen die zusätzliche Dependency nicht; eine 20-Zeilen-Regex-Heuristik reicht für die fünf relevanten Browser/OS-Kombinationen).
2. **Tags:**
   ```
   ┌─ Tags ──────────────────────────────────────┐
   │ [x] kontakt-team   [ ] buehne-techniker    │
   │ [x] funkdienst     [ ] entwickler          │
   │ [+ Neuen Tag erstellen]                     │
   └─────────────────────────────────────────────┘
   ```

### 7.4 Neue Seite: Tag-Management (`/sso-admin/tags/`)

Listet alle Django auth.Group im System (in dieser UI als "Tags" bezeichnet, damit dem Admin nicht zwei Konzepte für dasselbe Ding um die Ohren fliegen):

- Tabelle: Name, Mitgliederzahl, Created at.
- "+ Neuer Tag" Inline-Form.
- Klick öffnet Tag-Detail mit Mitgliederliste + Toggle-Buttons.

### 7.5 Sidebar-Eintrag

`/sso-admin/tags/` taucht im Admin-Sidebar-Block unter "SSO" auf, neben dem bestehenden "SSO Dashboard"-Link.

---

## 8. Security & Privacy

### 8.1 IP-Adressen sind PII

Speichern von IP-Adressen + Standort ist personenbezogene Datenverarbeitung. Für einen Vereins-internen IdP unter berechtigtem Interesse (Sicherheits-Monitoring) tragbar, aber:

- Retention: `prune_token_sessions` löscht TokenSession-Rows nach 30 Tagen Revoke/Expiry. Genug für "war das ich gestern" und "wer hat letzte Woche gepatzt", nicht so lang, dass es ein lebenslanges Bewegungsprofil wird.
- Audit-Log `SsoAuditLog` enthält ebenfalls IPs (bestehend); dort bleibt die Retention wie sie ist (kein automatisches Löschen — Audit ist append-only). Operatives Cleanup ist DB-Admin-Sache, nicht App-Logik.
- Keine Standort-Daten in Logs (Standard-Django-Logger nicht). Nur in DB.

### 8.2 Open-Redirect bei Policy-Wechsel

Nicht relevant — der Policy-Wechsel ist eine reine Admin-Aktion ohne Redirect-Komponente.

### 8.3 Tag-Leakage

Der `groups`-Claim wird heute jeder RP-App ausgehändigt, die den `groups`-Scope anfordert — komplett, kein Filter. Damit sieht jede App alle Tags eines Users. Heute kein Problem (keine sensiblen Tags), aber als **offene Frage** für später aufgenommen: pro-App Allow-Listing von Tag-Namen.

### 8.4 GeoIP-DB-Sicherheit

- Download über HTTPS (db-ip.com bietet TLS).
- Keine Signatur-Verifikation der DB (db-ip.com signiert nicht). Mitigation: DB-Inhalt wird ausschließlich vom `geoip2`-Reader gelesen, der defensiv ist (Format-Validierung, keine Code-Ausführung).
- Atomic Replace via `Path.replace()` — Halbschriebener Download crasht den Reader nicht (alter Reader hält bis zum nächsten Restart bzw. Reload).

### 8.5 CSRF / Permissions

- Alle neuen Views (Session-Revoke, Policy-Change, Tag-Toggle, Group-Membership-Toggle) sind `AdminOnlyMixin`-gated.
- POST-only für mutierende Operationen.
- Django CSRF-Schutz greift automatisch via bestehende Middleware.

### 8.6 Audit-Vollständigkeit

Nach Implementierung emittiert das System für jede der folgenden Aktionen einen `SsoAuditLog`-Eintrag:

- `LOGIN_SUCCESS` — Token wurde ausgestellt (existing EventType, bisher nicht emittiert; wird jetzt nachgeholt)
- `LOGIN_DENIED_NO_GRANT` / `LOGIN_DENIED_INACTIVE` (existing, bleibt)
- `GRANT_GIVEN` / `GRANT_REVOKED` (existing, bleibt)
- `APP_REGISTERED` / `APP_DELETED` (existing, bleibt)
- `TOKEN_REVOKED` (existing — cascade-induced)
- `SESSION_REVOKED` (NEU — admin-induced, einzelne Session)
- `APP_POLICY_CHANGED` (NEU)
- `GROUP_MEMBERSHIP_CHANGED` (NEU)

---

## 9. Testing

Strikte TDD via `superpowers:test-driven-development` (in Plan-Phase aktiv). Neue Test-Files:

### 9.1 `tests/test_sso_policy.py`

Matrix-Tests `user_can_access(user, app)` × 5 Policies × {Applicant, Member, Staff, Admin, Inactive User} × {grant present, grant absent, grant revoked}:

```python
@pytest.mark.parametrize("policy,level,is_active,has_grant,expected", [
    # GRANT_REQUIRED
    ("grant_required",   "applicant", True,  True,  True),
    ("grant_required",   "applicant", True,  False, False),
    ("grant_required",   "member",    True,  True,  True),
    ("grant_required",   "admin",     False, True,  False),
    # OPEN_TO_ALL
    ("open_to_all",      "applicant", True,  False, True),
    ("open_to_all",      "applicant", False, True,  False),
    # OPEN_TO_MEMBERS
    ("open_to_members",  "applicant", True,  False, False),
    ("open_to_members",  "member",    True,  False, True),
    ("open_to_members",  "staff",     True,  False, True),
    # OPEN_TO_INTERNAL
    ("open_to_internal", "member",    True,  False, False),
    ("open_to_internal", "staff",     True,  False, True),
    ("open_to_internal", "admin",     True,  False, True),
    # OPEN_TO_ADMINS
    ("open_to_admins",   "staff",     True,  False, False),
    ("open_to_admins",   "admin",     True,  False, True),
])
def test_user_can_access_matrix(policy, level, is_active, has_grant, expected):
    ...
```

Plus: Policy-Wechsel-Audit, `ApplicationPolicy`-Auto-Creation auf Save-Click (Default war GRANT_REQUIRED).

### 9.2 `tests/test_sso_sessions.py`

- `save_bearer_token` erzeugt TokenSession mit IP+UA aus Headers.
- Refresh-Rotation: parent_session.last_seen_at gebumpt, neue Session mit parent-FK.
- GeoIP-Lookup-Fallback: kein Crash bei fehlender DB, country/city bleiben leer.
- Admin-Revoke: RefreshToken.revoked gesetzt, AccessTokens expired, Session-Row updated, Audit-Row geschrieben.
- Admin-Revoke ist idempotent (zweiter Klick keine Wirkung).
- Cascade (User-Deactivate, Grant-Revoke): TokenSession.revoked_at gesetzt mit korrektem reason.
- prune_token_sessions: idempotent, löscht nur abgelaufene/revoked-old Rows.

### 9.3 `tests/test_sso_claims.py` (erweitert)

- `_build_groups` Matrix:
  - Applicant: keine membership_level-Group, aber Stations/Regions/Tags propagiert.
  - Member ohne Assignments/Tags: `["member"]`.
  - Member mit StationAssignment(role=admin) auf "oe5xrx-1": `["member", "station:oe5xrx-1:admin"]`.
  - Member in Django-Group "kontakt-team": `["member", "tag:kontakt-team"]`.
  - Vollbeispiel mit allen vier Quellen -> deterministische sortierte Liste.
- Token-Inhalt: ID-Token enthält `groups` claim wenn scope `groups` requested, andernfalls nicht.

### 9.4 `tests/test_sso_audit.py` (erweitert)

- LOGIN_SUCCESS wird beim ersten Token-Issue geschrieben, NICHT bei Refresh-Rotation.
- SESSION_REVOKED wird beim Admin-Revoke geschrieben.
- APP_POLICY_CHANGED wird beim Policy-Wechsel geschrieben.
- GROUP_MEMBERSHIP_CHANGED wird beim Tag-Toggle geschrieben.

### 9.5 `tests/test_sso_views.py` (erweitert)

- Policy-Wechsel-View ist admin-only, POST-only, idempotent.
- Session-Revoke-View dito.
- Tag-Management-Views dito.

### 9.6 `tests/test_sso_geoip.py`

- Test-Fixture: kleine mmdb-Datei mit 2 bekannten IPs.
- `lookup_location` returnt richtige Werte für bekannte IPs.
- `lookup_location` returnt (None, None) für unbekannte IPs, ohne zu raisen.
- `lookup_location` returnt (None, None) wenn DB-Datei fehlt.

### 9.7 `tests/test_sso_flow.py` (erweitert)

- Kompletter Auth-Code-Flow erzeugt eine TokenSession mit korrekter IP/UA.
- Folgender Refresh-Token-Flow erzeugt zweite TokenSession mit parent-FK auf erste; erste session.last_seen_at ist gebumpt.

### 9.8 `tests/test_sso_policy_migration.py`

- Migration ist additiv (keine bestehenden Daten betroffen).
- App ohne ApplicationPolicy-Row verhaelt sich wie GRANT_REQUIRED.

---

## 10. Migration & Deployment

### 10.1 DB-Migrationen

Drei neue Migrationen in `apps/sso/migrations/`:

- `0004_application_policy.py` — `ApplicationPolicy` Modell.
- `0005_token_session.py` — `TokenSession` Modell.
- `0006_extend_audit_event_types.py` — `AlterField` auf `SsoAuditLog.event_type.choices`, ergänzt `SESSION_REVOKED`, `APP_POLICY_CHANGED`, `GROUP_MEMBERSHIP_CHANGED`.

Alle drei sind additiv und brechen keine bestehenden Daten. Rollback (mit DOT-Migrations als Boden) ist sauber.

### 10.2 Settings + Compose-Diff

```python
# config/settings/base.py (neu)
GEOIP_DB_PATH = os.environ.get(
    "GEOIP_DB_PATH",
    str(BASE_DIR / "geoip_db" / "dbip-city-lite.mmdb"),
)
```

```yaml
# docker-compose.yml + deploy/docker-compose.prod.yml
services:
  web:
    volumes:
      - oidc_keys:/app/oidc_keys     # bestehend
      - geoip_db:/app/geoip_db       # NEU
volumes:
  oidc_keys:                          # bestehend
  geoip_db:                           # NEU
```

### 10.3 Deploy-Sequenz

1. **Migration laufen lassen** (Standard-Deploy-Hook).
2. **GeoIP-DB initial befüllen:**
   ```bash
   docker compose run --rm web python manage.py update_geoip_db
   ```
   Idempotent — falls die DB schon da ist, Overwrite ist OK.
3. **Cron-Job aufsetzen** für `update_geoip_db` (1×/Tag — Begründung siehe Section 6.3: db-ip-Release-Zeitpunkt innerhalb des Monats ist nicht garantiert, daily-Cron + Vormonats-Fallback kappt den Worst-Case-Lag auf ~1 Tag) und `prune_token_sessions` (1×/Tag). Beide laufen als GitHub-Actions-Cron — Details in Section 14.2.

### 10.4 Bestehende Sessions / Tokens

- Existierende RefreshTokens haben keine zugehörige `TokenSession`-Row. Das ist OK: sie sind ab der nächsten Refresh-Rotation gleichberechtigt im System, weil dann der `save_bearer_token`-Hook eine Session anlegt (mit `parent=None`).
- Die "Active Sessions" Tabelle des Users ist also kurzzeitig (max. 1 h, Access-Token-Lifetime) nicht 100% vollständig nach dem Deploy. Akzeptabel.

### 10.5 Requirements-Diff

Neue Python-Dependency:

```
# requirements/base.txt
geoip2>=4.8
```

`geoip2` hat selbst eine kleine Dependency-Liste (`maxminddb`), schlanke Library.

### 10.6 Dokumentation

- `README.md` bekommt einen "GeoIP-DB Setup"-Abschnitt mit dem `update_geoip_db`-Befehl.
- `docs/sso/group-propagation.md` neu — RP-Mapping-Konvention + Snippets für InvenTree, Grafana, Nextcloud.

---

## 11. Was bewusst NICHT drin ist (YAGNI)

- **Pro-App Tag-Filter** ("Tag `vorstand-privat` geht nicht an InvenTree raus"). Heute kein konkreter Bedarf. Notiert als V2 in Section 8.3.
- **Massen-Revoke beim Policy-Wechsel** ("alle aktiven Sessions dieser App rauskicken"). Explizit weggelassen; Admin macht das via Einzel-Revoke oder läuft die 14 Tage aus.
- **End-User-Self-Service** ("ich will meine Sessions selbst sehen / abwürgen können"). Nur Admin in V1. Wenn End-User-UX-Stream kommt, wird das ergänzt.
- **WebSocket-Live-Updates** der Session-Liste. Page-Reload reicht.
- **Geo-Lookup mit Stadt-Genauigkeit > Stadt** (Stadtteil, Straße). Brauchen wir nicht; db-ip.com Free gibt das auch gar nicht her.
- **MaxMind statt db-ip.com** als Default-Provider. Beide Formate sind Drop-in-kompatibel; wechseln ist 1 Zeile Config. Wir starten ohne License-Key-Aufwand.
- **UA-Parser-Library** (z.B. `ua-parser`). 20-Zeilen-Heuristik reicht. Lib ist 2 MB, hoher Wartungs-Overhead.
- **Group → automatische Permission-Eskalation im station-manager selbst** ("wer in Tag `funkdienst` ist, darf XY"). Würde eine zweite Quelle der Wahrheit neben membership_level + topology einführen — gegen die Architekturentscheidung aus PR #54-56. Tags sind **nur** Weitergabe-Mechanismus an externe Apps.

---

## 12. Offene Fragen für die Plan-Phase

- **Position der "Active Sessions"-Card auf User-Detail**: über oder unter den Station/Region-Assignment-Cards aus PR #56? Default-Vorschlag: direkt unter "App-Zugriffe", über den Assignment-Cards (Sessions sind volatiler, Admin schaut sie häufiger an).
- **oauthlib-Attribut für Parent-Refresh-Token**: in Section 4.2 nutzen wir `getattr(request, "refresh_token_instance", None)` für die Refresh-Rotation-Erkennung. Der exakte Attribut-Name muss in der Plan-Phase gegen die installierte DOT/oauthlib-Version verifiziert werden (DOT-internes API, in Tests fixiert).
- **UA-Heuristik-Format**: "FF/Linux" vs. "Firefox 127 / Ubuntu". Default-Vorschlag: kurze Form (Family/OS-Family), reicht für die schnelle Erkennung "ja, das ist mein Laptop".
- **Tag-Slug**: Django-Group.name ist freier String mit Spaces erlaubt. Brauchen wir einen Slug-Constraint (z.B. nur `[a-z0-9-]+`)? Default-Vorschlag: ja, validierung in der Tag-Erstellen-Form, damit Token-Strings vorhersagbar bleiben (`tag:kontakt-team` statt `tag:Kontakt Team`).
- **Session-Liste-Sortierung**: by last_seen_at desc oder issued_at desc? Default-Vorschlag: last_seen_at desc (aktuellste Aktivität oben).
- **prune_token_sessions Aufbewahrungsfrist**: 30d vs. 90d? Default-Vorschlag: 30d. Wenn jemand zurückblickt, will er typischerweise letzten Monat, nicht letztes Quartal.

Diese Fragen sind Detail-Entscheidungen, keine Spec-Blocker — die Plan-Phase legt fest.

---

## 13. Roll-Up Checkliste

Nach Merge dieses Specs sind folgende Capabilities live:

- [ ] Admin kann pro App eine von 5 Hausordnungen wählen (`grant_required` / `open_to_all` / `open_to_members` / `open_to_internal` / `open_to_admins`).
- [ ] Apps mit `open_to_*`-Policy brauchen keine Einzelfreischaltungen mehr.
- [ ] Pro Login wird eine TokenSession-Karteikarte angelegt mit IP, Land, Stadt, Browser, Zeitstempel.
- [ ] Admin sieht pro User und pro App eine Liste aktiver Sessions.
- [ ] Admin kann einzelne Sessions abwürgen.
- [ ] OIDC-`groups`-Claim ist befüllt mit `member`/`staff`/`admin` + `station:<slug>:<role>` + `region:<slug>:<role>` + `tag:<name>`.
- [ ] Admin kann Tags (Django auth.Group) im Custom-UI verwalten ohne Django-Admin-Backend.
- [ ] Dokumentation für RP-Operatoren ("So mappst du die Groups in InvenTree/Grafana") existiert.
- [ ] Audit-Log enthält für jede Sicherheits-relevante Aktion einen Eintrag (LOGIN_SUCCESS, SESSION_REVOKED, APP_POLICY_CHANGED, GROUP_MEMBERSHIP_CHANGED).
- [ ] GeoIP-DB von db-ip.com Free wird per Cron täglich aktualisiert (Vormonats-Fallback bei 404 für noch-nicht-publizierte Monats-Files).
- [ ] Test-Coverage: Policy-Matrix, Session-Lifecycle, Group-Synthese, GeoIP-Fallback, Cascade, Admin-Revoke-Idempotenz.
- [ ] Bestehender Login-Flow + Station-Agent Ed25519-Auth bleiben unverändert.
- [ ] Bestehende AppGrant-Mechanik bleibt unverändert (Default `GRANT_REQUIRED` ist abwärtskompatibel).
- [ ] Deployment-Änderungen in `servers`-Repo (siehe Section 14) sind merged und laufen.

---

## 14. Deployment-Änderungen im `servers`-Repo (separater PR)

Das Feature ist ohne Infrastruktur-Begleit-Änderungen nicht funktional. Diese Änderungen leben im **`servers`-Repo** (Terraform + Service-Compose-Manifests + GitHub-Actions-Workflows), nicht im station-manager-Repo. Sie werden **als separater PR** in `servers` umgesetzt — die beiden PRs (station-manager + servers) werden parallel entwickelt und können in beliebiger Reihenfolge mergen, aber beide müssen live sein, bevor das Feature End-User-sichtbar wird.

### 14.1 `services/station_manager/docker-compose.yml`

Drei kleine Anpassungen:

**a) `prepare-volumes`-init-Container** legt zusätzlich `geoip_db`-Subdirectory mit appuser-Ownership an (analog zum bestehenden `oidc_keys`):

```yaml
prepare-volumes:
  image: alpine:3
  user: "0:0"
  restart: "no"
  command:
    - /bin/sh
    - -c
    - >
      install -d -m 0700 -o 1000 -g 1000 /target/oidc_keys &&
      install -d -m 0750 -o 1000 -g 1000 /target/geoip_db &&
      echo "volumes ready"
  volumes:
    - /opt/oe5xrx-data/station_manager:/target
  networks:
    - station_manager-internal
```

**b) `web`-Service** bekommt zusätzliche Bind-Mount + Env-Variable:

```yaml
web:
  ...
  environment:
    <<: *station-manager-env
    GEOIP_DB_PATH: /app/geoip_db/dbip-city-lite.mmdb
  volumes:
    - /opt/oe5xrx-data/station_manager/oidc_keys:/app/oidc_keys
    - /opt/oe5xrx-data/station_manager/geoip_db:/app/geoip_db   # NEU
```

**c) Auch die Worker-Container** (`station-monitor`, `alert-monitor`, `background-worker`) brauchen den Mount NICHT — GeoIP-Lookup passiert nur im `web`-Container während Token-Issue. Cron-Tasks (siehe 14.2) reichen sich `--rm`-Web-Container an.

### 14.2 Cron-Workflows (analog `backup.yml`)

Das Repo nutzt **GitHub-Actions-Cron + self-hosted runner** als Cron-Mechanismus (keine systemd-Timer auf der VM, siehe `backup.yml` als Referenz-Pattern). Zwei neue Workflows:

**`.github/workflows/update-geoip-db.yml`** — einmal pro Tag, lädt (falls verfügbar) die frische db-ip.com City Lite DB. Begründung für Daily statt Monthly: db-ip.com veröffentlicht zwar monatlich, aber der Veröffentlichungs-Tag innerhalb des Monats schwankt — ein striktes Monthly-Cron am 1. um 04:00 UTC würde 404 holen und einen ganzen Monat alte Daten behalten (Worst Case: 2-3 Monate hinten nach). Daily-Cron + Vormonats-Fallback im Command (Section 6.3) kappt das auf max. 1 Tag Lag. Bandbreite: ~150 MB × 30 Tage ≈ 4.5 GB/Monat, vernachlässigbar.

```yaml
name: update-geoip-db
on:
  schedule:
    - cron: '0 4 * * *'   # 04:00 UTC täglich
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: update-geoip-db
  cancel-in-progress: false

jobs:
  guard:
    runs-on: ubuntu-latest
    steps:
      - name: assert ref is main
        env:
          REF: ${{ github.ref }}
        run: |
          if [ "$REF" != "refs/heads/main" ]; then
            echo "::error::update-geoip-db.yml may only run from main"
            exit 1
          fi

  update:
    needs: guard
    runs-on: [self-hosted, oe5xrx-prod-01]
    timeout-minutes: 10
    steps:
      - name: invoke update_geoip_db inside web container
        run: |
          cd /opt/oe5xrx-services/station_manager
          docker compose exec -T web python manage.py update_geoip_db
```

**`.github/workflows/prune-token-sessions.yml`** — einmal pro Tag, räumt alte revoked/expired TokenSession-Rows auf:

```yaml
name: prune-token-sessions
on:
  schedule:
    - cron: '20 3 * * *'   # 03:20 UTC, 20 min nach backup.yml
  workflow_dispatch:

# (gleiche guard+concurrency-Struktur wie oben)

jobs:
  prune:
    needs: guard
    runs-on: [self-hosted, oe5xrx-prod-01]
    timeout-minutes: 5
    steps:
      - run: |
          cd /opt/oe5xrx-services/station_manager
          docker compose exec -T web python manage.py prune_token_sessions
```

Beide Workflows nutzen `docker compose exec -T web` (analog dem schon existierenden Pattern, falls vorhanden — andernfalls wird `docker compose run --rm web` verwendet, das auch funktioniert, nur teurer im Container-Start).

### 14.3 Initial-Bootstrap der GeoIP-DB

Beim Erst-Deploy nach Merge muss die GeoIP-DB einmal manuell gezogen werden, sonst funktioniert der Lookup beim ersten Login-Versuch nicht (Fallback liefert "Unknown"). Zwei gleichwertige Wege:

1. **Workflow manuell triggern:** `gh workflow run update-geoip-db.yml -R OE5XRX/servers`.
2. **Direkt auf der VM** (via Workflow-Step im Rahmen des Deploy-PRs): einmaliger Aufruf von `docker compose exec`.

Option 1 ist der Default-Vorschlag — kein zusätzlicher Deploy-Step, klar dokumentiert in den Runbooks.

### 14.4 Doku in `services/station_manager/README.md` (falls existiert)

Kurzer Abschnitt:

- **Bind-Mounts** auf der VM: `/opt/oe5xrx-data/station_manager/{oidc_keys,geoip_db}` — beide 0700-700 für appuser=1000.
- **GeoIP-DB-Refresh** läuft als GitHub-Action-Cron `update-geoip-db.yml` (täglich, Begründung siehe Section 6.3). Manuelle Refreshes via `gh workflow run`.
- **TokenSession-Cleanup** läuft als GitHub-Action-Cron `prune-token-sessions.yml`.

### 14.5 Reihenfolge / Cross-Repo-Abhängigkeit

Welcher PR zuerst mergen kann:

- **`servers`-PR zuerst:** Compose-Update legt das geoip_db-Verzeichnis an, neue Env-Variable wird gesetzt, station-manager-Container restartet — der noch nicht vorhandene `apps/sso/geoip.py`-Code wird einfach nicht aufgerufen (station-manager-Code aus PR vorher kennt GEOIP_DB_PATH nicht). **Risiko:** keiner, der Mount ist harmlos.
- **`station-manager`-PR zuerst:** Code referenziert `GEOIP_DB_PATH`, das im Container-Env noch nicht gesetzt ist. Default-Fallback im Code (`os.environ.get(..., "/app/geoip_db/...")`) verweist auf einen nicht existierenden Pfad → Reader-Init schlägt fehl → Lookups returnen `(None, None)`. **Risiko:** Sessions kriegen leere Standort-Felder, bis der servers-PR landet. Nicht kritisch.

**Empfohlene Reihenfolge:** servers-PR zuerst (oder zumindest gleichzeitig). Vorteil: ab dem station-manager-Merge ist GeoIP-Tracking sofort aktiv.

### 14.6 Checkliste für den `servers`-PR

- [ ] `services/station_manager/docker-compose.yml`: `prepare-volumes` legt `geoip_db` Subdir an + `web`-Service bekommt Bind-Mount + `GEOIP_DB_PATH` Env.
- [ ] `.github/workflows/update-geoip-db.yml`: täglicher Cron (Begründung in Section 6.3), ref-guard auf `main`, läuft auf self-hosted runner.
- [ ] `.github/workflows/prune-token-sessions.yml`: täglicher Cron, gleiche Struktur.
- [ ] Optional: README-Erweiterung in `services/station_manager/`.
- [ ] Erstbefüllung-Trigger nach Merge: `gh workflow run update-geoip-db.yml`.
