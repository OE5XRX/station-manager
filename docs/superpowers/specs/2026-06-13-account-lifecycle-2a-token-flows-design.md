# Account Lifecycle — Sub-Spec 2a: Token-Email-Flows

**Status:** Draft, brainstormed 2026-06-13.
**Bogen:** Erster Sub-Spec des Account-Lifecycle-Arcs (Sub-Spec #2 des Identity-Themas, nach abgeschlossenem User-Domain-Redesign 1a/1b/1c). Sub-Spec 2b "Soft-Delete" folgt mit eigenem Spec.
**Branch:** `feat/account-lifecycle-2a-token-flows` (von `main`, nach Merge von 1a/1b/1c + #74).
**Ziel:** Drei tokenbasierte Email-Flows einführen, die heute fehlen oder hand-gepatched sind:

1. **Welcome-Mail mit Set-Password-Link** — Admin legt neuen User nur mit Identity-Feldern an; der User erhält einen Mail-Link und setzt selbst sein Passwort. Heute tippt der Admin im UserCreationForm ein Passwort, was klobig ist und das Passwort durch Admin-Hände schleust.
2. **Password-Reset (forgot password)** — User klickt auf der Login-Seite "Passwort vergessen", erhält per Mail einen Link, setzt ein neues Passwort. Heute gibt es nur den authentifizierten Self-Service-Change.
3. **Email-Verification bei Änderung** — Wenn ein User im Profil seine Email-Adresse ändert, geht ein Verify-Link an die NEUE Adresse; die alte bleibt aktiv bis der Link geklickt wird. Schutz gegen Account-Takeover.

Alle drei Flows teilen sich ein Token-Modell + einen Email-Helper + die Set-Password-View. Nach Merge dieses Specs kann der Verein User onboarden ohne Klartext-Passwörter durch Admin-Hände, kann jeder User sein Passwort selbst zurücksetzen, und Email-Änderungen sind gegen Session-Hijacking abgesichert.

---

## 1. Kontext

Voraussetzungen sind alle bereits auf `main`:

- **User-Domain-Redesign 1a/1b/1c** ist komplett gemergt (PRs #69/#70/#71). User-Modell hat alle Profile-Felder, ProfileView ist Multi-Form-TemplateView mit 4 Panels (Identity / Profil / Adresse / Passwort), UserCreateView/UserUpdateView emittieren `USER_CREATED`/`USER_UPDATED`/`USER_ACTIVATED`/`USER_DEACTIVATED`-Audits.
- **#74 LoginRequiredMiddleware** ist live (PR #74) — alle neuen Views sind by-default login-required, anonyme Endpoints brauchen explizit `@method_decorator(login_not_required, name="dispatch")`. Pattern aus `apps/accounts/views.py:LoginView` + `apps/api/views.py:HealthCheckView` ist etabliert.
- **Brevo SMTP** ist in `config/settings/base.py` konfiguriert (`EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL`). In `dev.py` wird `console.EmailBackend` benutzt. `apps/monitoring/notifications.py` ist heute der einzige Email-Sender und ruft `django.core.mail.send_mail` direkt mit Plain-Text.
- **django-axes** ist im Login-Pfad aktiv (`axes.middleware.AxesMiddleware`). Login-Lockout pro IP / pro User.

Was heute fehlt und dieser Spec adressiert:

- **Welcome:** UserCreationForm hat `password1`/`password2` Felder. Beim Save setzt Django das Passwort sofort. Es gibt keinen Email-Versand, keine Erstkontakt-Mail, kein "Hier ist dein Link, setz dein Passwort"-Pattern.
- **Reset:** Es gibt keinen "Passwort vergessen"-Link auf der Login-Page und keinen Endpoint dafür. Ein User mit verlorenem Passwort muss sich an einen Admin wenden.
- **Verify:** ProfileIdentityForm speichert Email-Änderungen sofort in `user.email`. Wenn jemand eine Session hijackt, kann er die Email umstellen und ist dann der Account-Owner. Auch versehentliche Tippfehler in der Email-Adresse sperren den User aus (er bekommt keine Reset-Mails mehr, weil die Adresse falsch ist).

**Out of Scope (siehe §10):**
- HTML-Email-Templates — separater Pass über *alle* Email-Sender inkl. monitoring.
- Soft-Delete — Sub-Spec 2b.
- Member-Approval-Flow (Applicant beantragt selbst Mitgliedschaft) — separater Spec.
- i18n der Email-Templates.

---

## 2. Datenmodell

### 2.1 `AccountToken` (`apps/accounts/models.py`)

```python
import secrets
import hashlib
from datetime import timedelta
from django.db import models
from django.db.models import TextChoices
from django.utils import timezone


class AccountToken(models.Model):
    """Single-use, time-limited token for Welcome / Reset / Verify flows.

    The raw token is generated via ``secrets.token_urlsafe(32)`` and is
    only ever returned from ``issue_token``; the DB stores only the
    SHA-256 hash. Consumption is atomic: ``consume_token`` does a
    SELECT FOR UPDATE on the row and sets ``used_at`` in the same
    transaction, so a parallel request cannot redeem the same token.
    """

    class TokenType(TextChoices):
        WELCOME = "welcome", "Welcome (set initial password)"
        RESET = "reset", "Password reset"
        VERIFY = "verify", "Email verification"

    EXPIRY = {
        TokenType.WELCOME: timedelta(days=7),
        TokenType.RESET: timedelta(hours=24),
        TokenType.VERIFY: timedelta(hours=24),
    }

    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="account_tokens",
    )
    token_type = models.CharField(
        max_length=16,
        choices=TokenType.choices,
        db_index=True,
    )
    secret_hash = models.CharField(max_length=64)  # SHA-256 hex
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    ip_created = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "token_type", "used_at"]),
            models.Index(fields=["secret_hash"]),  # lookup by hash
        ]

    def is_active(self):
        return self.used_at is None and self.expires_at > timezone.now()
```

**Felder im Detail:**

- `secret_hash` — SHA-256-Hex des Raw-Tokens. Der Raw-Token wird einmalig zurückgegeben (siehe §2.2), nie in DB persistiert. Hash → Raw ist nicht umkehrbar, so bleibt selbst ein Read-Access-Breach harmlos.
- `payload` — JSON-Blob für token-typ-spezifische Daten. Für `VERIFY`: `{"new_email": "neue@adr.org"}`. Für `WELCOME`/`RESET`: leer `{}`.
- `ip_created` — IP der ausstellenden Request (Admin-IP bei Welcome, anon-IP bei Reset, User-IP bei Verify). Für Audit + Rate-Limit-Forensik.
- `used_at` — `None` solange ungeused. Beim Consume gesetzt; ein Token kann nicht zweimal redeemed werden.

**Migration:** `0XXX_account_token.py` — neue Tabelle, keine FK-Änderungen am `User`. Standard-Django-Migration.

### 2.2 Helper-Modul `apps/accounts/tokens.py` (neu)

```python
def issue_token(user, token_type, payload=None, ip=None):
    """Generate a fresh raw token, persist its hash, return the raw token.

    Caller MUST embed the raw token in a URL and send it to the user.
    After this call, the raw is gone from server memory unless caller
    holds the return value.
    """
    raw = secrets.token_urlsafe(32)
    AccountToken.objects.create(
        user=user,
        token_type=token_type,
        secret_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=timezone.now() + AccountToken.EXPIRY[token_type],
        payload=payload or {},
        ip_created=ip,
    )
    return raw


def consume_token(raw, expected_type):
    """Atomically validate + mark used. Returns the token row or None.

    None is returned if: token doesn't exist, wrong type, already used,
    or expired. Callers SHOULD NOT differentiate the failure cause to
    the end-user (timing-safe error).
    """
    secret_hash = hashlib.sha256(raw.encode()).hexdigest()
    with transaction.atomic():
        try:
            token = (
                AccountToken.objects
                .select_for_update()
                .get(secret_hash=secret_hash, token_type=expected_type)
            )
        except AccountToken.DoesNotExist:
            return None
        if not token.is_active():
            return None
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        return token


def invalidate_pending_tokens(user, token_type):
    """Mark all unused tokens of `token_type` for `user` as used.

    Called when a fresh token of the same type is issued, so a user
    can only redeem the most recent one. Idempotent.
    """
    AccountToken.objects.filter(
        user=user, token_type=token_type, used_at__isnull=True
    ).update(used_at=timezone.now())
```

`issue_token` und `invalidate_pending_tokens` müssen in derselben Transaktion laufen, damit Caller nicht versehentlich ein neues Token ausstellen und dann einen Race-failure beim Invalidate haben. Konvention: Caller wrapt das in `transaction.atomic()` (siehe Flow-Beispiele in §3).

---

## 3. Flows

### 3.1 Welcome-Flow

**UserCreateView.form_valid:**

```python
def form_valid(self, form):
    with transaction.atomic():
        user = form.save(commit=False)
        user.set_unusable_password()
        user.save()
        # USER_CREATED zuerst — der Audit-Feed liest sich dann
        # chronologisch Create → Welcome (created_at-ordering).
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            actor=self.request.user,
            target_user=user,
            message=f"{user.username} <{user.email}>",
            ip_address=_client_ip(self.request),
        )
        raw = issue_token(
            user, AccountToken.TokenType.WELCOME, ip=_client_ip(self.request)
        )
        # NB: alles in einer Transaktion. Wenn send_account_email failt,
        # rollt der komplette user-create + Audit + Token-Issue zurück —
        # gewünscht, der User kann sich sonst nie einloggen und wäre
        # auch nicht reissue-bar ohne Manual-Cleanup.
        send_account_email(user, "welcome", {
            "raw_token": raw,
            "actor": self.request.user.username,
        })
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.WELCOME_TOKEN_SENT,
            actor=self.request.user,
            target_user=user,
            message=f"to {user.email}",
            ip_address=_client_ip(self.request),
        )
    self.object = user
    messages.success(
        self.request, _("User created. Welcome link sent to %(email)s.") % {"email": user.email}
    )
    return redirect(self.get_success_url())
```

**Wichtig:** Die `messages.success` enthält die Email-Adresse — gibt dem Admin Feedback wohin die Mail gegangen ist (besonders relevant wenn Brevo-Bounce silent ist).

**SetPasswordView (geteilt mit Reset, §3.2):**

```python
@method_decorator(login_not_required, name="dispatch")
class SetPasswordView(View):
    template_name = "accounts/set_password.html"

    def get(self, request, token):
        # Token-Type ist nicht in der URL — wir matchen erst WELCOME,
        # dann RESET. Beide haben semantisch dieselbe Wirkung.
        # Wenn keiner matcht: Generic-Error-Page.
        token_row = self._lookup(token)
        if token_row is None:
            return self._invalid_response(request)
        form = SetPasswordForm(user=token_row.user)
        return render(request, self.template_name, {"form": form, "token": token})

    def post(self, request, token):
        token_row = self._lookup(token)
        if token_row is None:
            return self._invalid_response(request)
        form = SetPasswordForm(user=token_row.user, data=request.POST)
        if form.is_valid():
            consume_token(token, token_row.token_type)  # atomic mark used
            form.save()  # writes password hash
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.PASSWORD_SET_FROM_TOKEN,
                actor=token_row.user,
                target_user=token_row.user,
                message=f"via {token_row.token_type}",
                ip_address=_client_ip(request),
            )
            auth_login(request, token_row.user)
            messages.success(request, _("Password set. Welcome to OE5XRX."))
            return redirect("accounts:profile")
        return render(request, self.template_name, {"form": form, "token": token})

    def _lookup(self, raw):
        # GET-Phase: lookup ohne consume (consume erst beim erfolgreichen
        # POST). Wir matchen jeden Token-Type, den Set-Password verarbeiten
        # darf.
        secret_hash = hashlib.sha256(raw.encode()).hexdigest()
        return AccountToken.objects.filter(
            secret_hash=secret_hash,
            token_type__in=[
                AccountToken.TokenType.WELCOME,
                AccountToken.TokenType.RESET,
            ],
            used_at__isnull=True,
            expires_at__gt=timezone.now(),
        ).first()

    def _invalid_response(self, request):
        messages.error(
            request,
            _("Link invalid or expired. Ask an admin for a new Welcome link, "
              "or request a password reset."),
        )
        return redirect("accounts:login")
```

**Subtilität:** Der GET-Lookup darf den Token NICHT konsumieren. Sonst öffnet jeder Email-Client mit Link-Vorschau (z.B. Outlook-Safe-Links, Mimecast) das Token vor dem User. Erst die POST-Aktion (User hat aktiv das Form abgeschickt) markiert `used_at`.

**Re-Send-Welcome (Admin-Action):**

Auf `user_detail.html` zeigt eine Card "Pending Welcome" wenn `not user.has_usable_password()`:

```html
{% if not target_user.has_usable_password %}
  <form method="post" action="{% url 'accounts:resend_welcome' target_user.pk %}">
    {% csrf_token %}
    <button class="btn btn-warning">{% trans "Resend Welcome Mail" %}</button>
  </form>
{% endif %}
```

`ResendWelcomeView` (AdminRequiredMixin, POST-only):

```python
class ResendWelcomeView(AdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        user = get_object_or_404(User, pk=pk)
        if user.has_usable_password():
            messages.error(request, _("User already has a password set."))
            return redirect("accounts:user_detail", pk=pk)
        with transaction.atomic():
            invalidate_pending_tokens(user, AccountToken.TokenType.WELCOME)
            raw = issue_token(
                user, AccountToken.TokenType.WELCOME, ip=_client_ip(request)
            )
            send_account_email(user, "welcome", {
                "raw_token": raw, "actor": request.user.username,
            })
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.WELCOME_TOKEN_SENT,
                actor=request.user,
                target_user=user,
                message=f"resend to {user.email}",
                ip_address=_client_ip(request),
            )
        messages.success(request, _("Welcome link re-sent."))
        return redirect("accounts:user_detail", pk=pk)
```

### 3.2 Password-Reset-Flow

**Login-Page-Link:** In `apps/accounts/templates/accounts/login.html` unter den Login-Buttons:

```html
<a href="{% url 'accounts:password_reset_request' %}" class="t-muted t-sm">
  {% trans "Forgot password?" %}
</a>
```

**PasswordResetRequestView:**

```python
@method_decorator(login_not_required, name="dispatch")
class PasswordResetRequestView(View):
    template_name = "accounts/password_reset_request.html"

    def get(self, request):
        return render(request, self.template_name, {"form": PasswordResetRequestForm()})

    def post(self, request):
        form = PasswordResetRequestForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"form": form})

        email = form.cleaned_data["email"].strip()
        ip = _client_ip(request)

        # Rate-limit: per-IP first (cheap, no DB lookup)
        if _ip_rate_exceeded(ip):
            self._audit_rate_limited(request, email, ip, reason="ip")
            return self._generic_success(request)

        # User lookup (timing-safe: same response either way)
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            return self._generic_success(request)

        # Per-user rate-limit
        if _user_rate_exceeded(user):
            self._audit_rate_limited(request, email, ip, target_user=user, reason="user")
            return self._generic_success(request)

        with transaction.atomic():
            invalidate_pending_tokens(user, AccountToken.TokenType.RESET)
            raw = issue_token(user, AccountToken.TokenType.RESET, ip=ip)
            send_account_email(user, "reset", {"raw_token": raw})
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.PASSWORD_RESET_REQUESTED,
                actor=None,
                target_user=user,
                message=f"to {email} from {ip}",
                ip_address=ip,
            )

        return self._generic_success(request)

    def _generic_success(self, request):
        messages.info(
            request,
            _("If %(email)s is a registered account, a password reset link "
              "has been sent. Check your inbox.") % {"email": request.POST.get("email", "")},
        )
        return redirect("accounts:login")

    def _audit_rate_limited(self, request, email, ip, target_user=None, reason=""):
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.PASSWORD_RESET_RATE_LIMITED,
            actor=None,
            target_user=target_user,
            message=f"{reason} {email} from {ip}",
            ip_address=ip,
        )
```

**Rate-Limit-Helper:**

```python
# in apps/accounts/throttle.py
from django.core.cache import cache

_IP_KEY = "pwreset:ip:{ip}"
_IP_LIMIT = 10
_IP_WINDOW = 3600  # 1h sliding via cache TTL

def _ip_rate_exceeded(ip):
    key = _IP_KEY.format(ip=ip)
    n = cache.get(key, 0)
    if n >= _IP_LIMIT:
        return True
    # incr/set with TTL only on first hit
    if n == 0:
        cache.set(key, 1, timeout=_IP_WINDOW)
    else:
        cache.incr(key)
    return False


_USER_LIMIT = 3
_USER_WINDOW = timedelta(hours=1)

def _user_rate_exceeded(user):
    count = AccountToken.objects.filter(
        user=user,
        token_type=AccountToken.TokenType.RESET,
        created_at__gt=timezone.now() - _USER_WINDOW,
    ).count()
    return count >= _USER_LIMIT
```

**Subtilität:** Der IP-Rate-Limit ist ein simpler Counter mit TTL — nicht ein perfektes Sliding-Window, aber gut genug für die Use-Case. In Prod ist der Cache Redis (persistent über Restarts), in Tests `LocMemCache` (per-test, ok).

**Set-Password-Phase:** identisch zum Welcome-Flow (§3.1), die SetPasswordView akzeptiert beide Token-Typen.

### 3.3 Email-Verify-Flow

Verantwortlichkeits-Aufteilung: **die Form** kümmert sich nur darum, dass `user.email` nicht persistiert wird wenn die Email geändert wurde. **Die View** (`ProfileView._save_identity`) erkennt die Änderung an `form.changed_data`, triggert die Verify-Mail und die Audit-Events. Damit bleibt der Form-Save trocken, ohne Request-Kopplung oder Side-Channel-Attribute.

**ProfileIdentityForm.save:**

```python
class ProfileIdentityForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "language")
        # ... widgets unchanged

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if User.objects.exclude(pk=self.instance.pk).filter(email__iexact=email).exists():
            raise forms.ValidationError(_("Another user already has this email."))
        return email

    def save(self, commit=True):
        """If email changed, persist all other fields but KEEP the DB-email
        unchanged. The view picks up form.changed_data and triggers the
        verify-flow separately.
        """
        if "email" in self.changed_data:
            # ModelForm._post_clean set self.instance.email = new_email
            # during is_valid(). Revert it so super().save() doesn't write
            # the new value.
            old_email = type(self.instance).objects.values_list(
                "email", flat=True
            ).get(pk=self.instance.pk)
            self.instance.email = old_email
        return super().save(commit=commit)
```

**`ProfileView._save_identity` Patch:**

```python
def _save_identity(self, request, user):
    form = ProfileIdentityForm(request.POST, instance=user, prefix="identity")
    if form.is_valid():
        changed = set(form.changed_data)
        new_email = form.cleaned_data.get("email") if "email" in changed else None
        form.save()  # email field NOT persisted if changed
        # USER_UPDATED audit excludes "email" — that change is still pending.
        self._emit_user_updated(request, user, changed - {"email"})
        if new_email:
            self._trigger_email_verify(request, user, new_email)
            messages.info(
                request,
                _("Confirmation link sent to %(new)s. Until you click it, %(old)s stays active.")
                % {"new": new_email, "old": user.email},
            )
        if changed - {"email"}:
            messages.success(request, _("Identity updated."))
    else:
        for errors in form.errors.values():
            messages.error(request, "; ".join(errors))
    return redirect("accounts:profile")


def _trigger_email_verify(self, request, user, new_email):
    with transaction.atomic():
        invalidate_pending_tokens(user, AccountToken.TokenType.VERIFY)
        raw = issue_token(
            user,
            AccountToken.TokenType.VERIFY,
            payload={"new_email": new_email},
            ip=_client_ip(request),
        )
        send_account_email(user, "verify", {
            "raw_token": raw,
            "new_email": new_email,
            "old_email": user.email,
            "override_to": new_email,
        })
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.EMAIL_VERIFY_REQUESTED,
            actor=user,
            target_user=user,
            message=f"{user.email} → {new_email}",
            ip_address=_client_ip(request),
        )
```

**Subtilität — `_post_clean`-Mutation:** ModelForm's `_post_clean` (läuft inside `full_clean()`/`is_valid()`) ruft `construct_instance` und mutiert `self.instance.email = new_email`. Wir setzen das in `save()` zurück auf das DB-original VOR `super().save()`. Damit landet `old_email` in der DB. Die other-fields-update (first_name etc.) klappt normal weil `super().save(commit=True)` Update-all-fields macht — und wir haben email-im-instance zurückgesetzt.

Alternative wäre `update_fields=[fields ohne email]`, ist aber fragiler wenn neue Felder dazukommen.

**VerifyEmailView:**

```python
@method_decorator(login_not_required, name="dispatch")
class VerifyEmailView(View):
    """GET-only: visit the link, email swaps, user sees confirmation."""
    http_method_names = ["get"]

    def get(self, request, token):
        token_row = consume_token(token, AccountToken.TokenType.VERIFY)
        if token_row is None:
            messages.error(request, _("Email verification link invalid or expired."))
            return redirect("accounts:login")

        new_email = token_row.payload.get("new_email", "")
        old_email = token_row.user.email
        if not new_email:
            # defensive — should never happen, payload is set on issue
            messages.error(request, _("Email verification token has no target address."))
            return redirect("accounts:login")

        # Check that no other user has grabbed this email in the meantime
        if User.objects.exclude(pk=token_row.user.pk).filter(email__iexact=new_email).exists():
            messages.error(
                request,
                _("Cannot verify: another account is already using %(email)s.") % {"email": new_email},
            )
            return redirect("accounts:profile" if request.user.is_authenticated else "accounts:login")

        token_row.user.email = new_email
        token_row.user.save(update_fields=["email"])
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.EMAIL_VERIFIED,
            actor=token_row.user,
            target_user=token_row.user,
            message=f"{old_email} → {new_email}",
            ip_address=_client_ip(request),
        )
        messages.success(
            request, _("Email updated to %(email)s.") % {"email": new_email}
        )
        if request.user.is_authenticated and request.user.pk == token_row.user.pk:
            return redirect("accounts:profile")
        return redirect("accounts:login")
```

**GET vs POST:** Verify ist absichtlich GET-only — der Klick auf einen Mail-Link ist immer ein GET, und der Effekt ist idempotent (Token wird konsumiert, zweiter Klick zeigt "invalid"). Das löst auch das Outlook-Safe-Links-Problem nicht (das prüfen würde den Link vorab auflösen), aber die Wahrscheinlichkeit dass ein Email-Scanner exakt diesen URL-Pfad öffnet und den Verify auslöst ist bei Verein-internen Mailservern akzeptabel.

Wenn das später ein Problem wird: zwei-Stufen-Verify mit GET = "Confirm screen", POST = "Verify" (analog Set-Password).

---

## 4. URLs

`apps/accounts/urls.py` neu:

```python
path("password-reset/",          PasswordResetRequestView.as_view(),
                                  name="password_reset_request"),
path("set-password/<str:token>/", SetPasswordView.as_view(),
                                  name="set_password"),
path("verify-email/<str:token>/", VerifyEmailView.as_view(),
                                  name="verify_email"),
path("users/<int:pk>/welcome/",   ResendWelcomeView.as_view(),
                                  name="resend_welcome"),
```

Decorator-Übersicht:

| URL | Decorator | Auth-Modell |
|---|---|---|
| `password_reset_request` | `@login_not_required` | Anon: ist gerade ausgesperrt. |
| `set_password` | `@login_not_required` | Anon ODER eingeloggt. Welcome-User ist anon, Reset-User ist anon, ggf. ein eingeloggter User klickt einen Reset-Link → ist OK, wird re-set + bleibt logged in (auth_login replaced session). |
| `verify_email` | `@login_not_required` | Eingeloggter User klickt Verify-Link (typisch), aber Anon kann auch klicken (z.B. wenn Session abgelaufen ist) — Effekt identisch. |
| `resend_welcome` | (kein Decorator) | `AdminRequiredMixin` — nur Admin darf. |

---

## 5. Forms

### 5.1 `UserCreationForm` — Passwort-Felder weg

```python
class UserCreationForm(forms.ModelForm):
    """Form for admins to create new users.

    NO password fields — the new user gets a Welcome mail with a
    Set-Password link. Until they click it, ``user.has_usable_password()``
    is False and login is impossible.
    """

    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "language")
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control", "required": True}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        if not email:
            raise forms.ValidationError(_("Email is required for the Welcome link."))
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError(_("A user with this email already exists."))
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_unusable_password()
        if commit:
            user.save()
        return user
```

Das `clean_email` macht zwei Dinge die der alte Form nicht hatte: (a) Email-Required (sonst kein Welcome möglich), (b) Email-Unique via `__iexact` (case-insensitive — Django's default email-field hat keinen unique-Constraint, und wir wollen "Hans@Example.org" nicht zweimal). Die Original-Schreibweise bleibt erhalten (siehe §8.6).

**Hinweis:** Sub-Spec 2b's Soft-Delete wird hier u.U. das Unique-Check anpassen müssen (soft-deleted Users behalten ihre Email aber sind nicht aktiv → wollen wir Reuse erlauben?). Wird in 2b geklärt.

### 5.2 `PasswordResetRequestForm` (neu)

```python
class PasswordResetRequestForm(forms.Form):
    email = forms.EmailField(
        label=_("Email"),
        widget=forms.EmailInput(attrs={
            "class": "form-control",
            "autofocus": True,
            "autocomplete": "email",
        }),
    )
```

Keine User-Existenz-Validation hier — die ist in der View, damit die Form-Validierung nicht Info leakt (form.is_valid → User existiert vs. form.is_valid → User existiert nicht hätten unterschiedliche Response-Pfade).

### 5.3 `SetPasswordForm` (neu)

```python
from django.contrib.auth.forms import SetPasswordForm as DjangoSetPasswordForm


class SetPasswordForm(DjangoSetPasswordForm):
    """Bootstrap-styled overlay over Django's SetPasswordForm.

    Django's class validates ``new_password1`` matches ``new_password2``
    AND runs all ``AUTH_PASSWORD_VALIDATORS``. Save sets the hash.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
```

### 5.4 `ProfileIdentityForm` — Verify-Flow

Siehe §3.3. Form-Verantwortung: `clean_email` prüft Cross-User-Uniqueness; `save()` schreibt alles außer `email` bei Email-Change. Trigger der Verify-Mail liegt in `ProfileView._save_identity` (kein `request=…`-Kwargs-Hack mehr). Form-Signatur bleibt Standard-ModelForm.

---

## 6. Email-Helper

### 6.1 `apps/accounts/emails.py` (neu)

```python
"""Single point of email-dispatch for account-lifecycle flows.

All three flows (welcome / reset / verify) render plain-text templates.
When we later migrate to HTML+plain multi-part mails, this is the only
module that changes — all callers remain on ``send_account_email``.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse


def _absolute_link(request_or_site, url_name, **kwargs):
    """Compose an absolute URL for an email-link.

    In production we use ``settings.SITE_URL`` (https://remote.oe5xrx.org),
    not request.build_absolute_uri — emails go out from background paths
    (e.g. ProfileIdentityForm.save) that don't always have a request, and
    SITE_URL is the canonical base for all transactional links.
    """
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    path = reverse(url_name, kwargs=kwargs)
    return f"{base}{path}"


def send_account_email(user, kind, context):
    """Dispatch a templated plain-text email to the user.

    kind: "welcome" | "reset" | "verify"
    context: dict for template rendering. Must contain "raw_token". May
             contain "override_to" (verify uses this to send to NEW email
             instead of user.email), "actor" (welcome shows admin user),
             "new_email" / "old_email" (verify).
    """
    raw_token = context.pop("raw_token")
    if kind == "verify":
        link = _absolute_link(None, "accounts:verify_email", token=raw_token)
    else:
        link = _absolute_link(None, "accounts:set_password", token=raw_token)

    ctx = {
        "user": user,
        "link": link,
        "site_url": getattr(settings, "SITE_URL", ""),
        **context,
    }
    subject = render_to_string(f"accounts/emails/{kind}.subject.txt", ctx).strip()
    body = render_to_string(f"accounts/emails/{kind}.body.txt", ctx)
    to_email = context.get("override_to") or user.email
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
    )
```

### 6.2 Templates (`templates/accounts/emails/`)

6 Files (3 × subject + body):

`welcome.subject.txt`:
```
Welcome to OE5XRX — set your password
```

`welcome.body.txt`:
```
Hi {{ user.first_name|default:user.username }},

{{ actor|default:"An admin" }} just created your OE5XRX account.

Set your password and start using the station-manager:
{{ link }}

This link is valid for 7 days. If you didn't expect this email, you can
safely ignore it.

73,
The OE5XRX team
{{ site_url }}
```

`reset.subject.txt`:
```
Password reset for your OE5XRX account
```

`reset.body.txt`:
```
Hi {{ user.first_name|default:user.username }},

Someone requested a password reset for your account on OE5XRX.

Set a new password here:
{{ link }}

This link is valid for 24 hours. If you didn't request this, you can
ignore this email — your current password stays unchanged.

73,
The OE5XRX team
{{ site_url }}
```

`verify.subject.txt`:
```
Confirm your new email for OE5XRX
```

`verify.body.txt`:
```
Hi {{ user.first_name|default:user.username }},

You requested to change your OE5XRX account email
from {{ old_email }} to {{ new_email }}.

Confirm the change:
{{ link }}

This link is valid for 24 hours. Until you click it, your old address
({{ old_email }}) remains active for login and notifications.

If you didn't request this, ignore this email — no change will happen.

73,
The OE5XRX team
{{ site_url }}
```

### 6.3 `SITE_URL`-Setting

`config/settings/base.py`:

```python
SITE_URL = os.environ.get("SITE_URL", "https://remote.oe5xrx.org")
```

In dev wird das auf `http://localhost:8000` gesetzt (`config/settings/dev.py`), in prod ist es das echte HTTPS-URL. Bitwarden-Secret `SITE_URL` in `oe5xrx-station_manager` Project, aber mit dem Default ist auch prod-only ok — Server-Yaml dokumentiert es als optional.

---

## 7. Audit-Events

Neue `EventType` Choices in `AccountAuditLog`:

```python
class EventType(models.TextChoices):
    # ... existing ...
    WELCOME_TOKEN_SENT          = "welcome_token_sent",         _("Welcome Token Sent")
    PASSWORD_RESET_REQUESTED    = "password_reset_requested",   _("Password Reset Requested")
    PASSWORD_RESET_RATE_LIMITED = "password_reset_rate_limited",_("Password Reset Rate Limited")
    PASSWORD_SET_FROM_TOKEN     = "password_set_from_token",    _("Password Set From Token")
    EMAIL_VERIFY_REQUESTED      = "email_verify_requested",     _("Email Verify Requested")
    EMAIL_VERIFIED              = "email_verified",             _("Email Verified")
```

Beobachtungen:

- `actor=None` ist legitim für `PASSWORD_RESET_REQUESTED` und `PASSWORD_RESET_RATE_LIMITED` — der anonyme Reset-Requester hat keine User-Identität. Das `AccountAuditLog.actor`-Feld muss `null=True` sein (ist es bereits seit 1a).
- `PASSWORD_RESET_RATE_LIMITED` wird auch geloggt wenn die Email zu KEINEM bekannten User gehört — dann mit `target_user=None`. Forensisch wertvoll: "jemand spammt Reset-Requests für unbekannte Emails von dieser IP" ist ein Probe-Versuch.
- `WELCOME_TOKEN_SENT` und `EMAIL_VERIFY_REQUESTED` enthalten die Email-Adresse in der `message`, damit der Audit-Feed durchsuchbar ist nach "wer hat eine Welcome-Mail bekommen, wann".

---

## 8. Sicherheit

### 8.1 Timing-safe User-Lookup

Im Password-Reset-Endpoint:

- Erfolgreich + nicht-existierender User produzieren BEIDE die identische Success-Response (`messages.info("If <email> is a registered account…")`).
- Kein Sleep / Jitter — `User.objects.get(email__iexact=…)` über einen DB-Lookup auf einem indexed-Field ist gleichmäßig schnell. Email-Send-Latency dominiert eh.
- Wenn `User.objects.get` einen `DoesNotExist` wirft, wird **kein Token erzeugt** und **keine Mail gesendet** — aber die Response-Latency ist nur um den Mail-Send-Pfad anders. Da Brevo SMTP-Send hinter SMTP-pooling läuft und nicht inline-blocking ist (oder optional `EMAIL_BACKEND=django.core.mail.backends.smtp` mit kurzem timeout), ist die Latency-Differenz < 100ms und für einen Timing-Attack über eine geographisch wechselnde IP nicht ausnutzbar.

### 8.2 Rate-Limit-Strategie

| Limit | Wert | Mechanismus | Storage |
|---|---|---|---|
| Per-IP Reset-Requests | 10 / 1h | Counter mit TTL | Redis (prod), LocMem (test) |
| Per-User Reset-Tokens | 3 / 1h | `AccountToken.objects.count()` | DB |

Begründung:
- Per-IP first weil cheap (cache-lookup, kein DB).
- Per-User second weil eine NAT/VPN-IP teilt sich viele User — IP-Limit allein erlaubt einem User Spam an einen anderen.
- Beide Limits geben Generic-Success zurück + audit-loggen — kein Info-Leak an den Angreifer ob er ge-limited wurde.

**axes-Interaktion:** Password-Reset bypassed axes — auch ein gelockter Account muss Reset machen können (sonst kann der User sich selbst nicht entsperren). axes-Counter wird beim erfolgreichen Set-Password vom Reset-Token **nicht** explizit reset; aber beim ersten erfolgreichen Login danach reset axes sich selbst.

### 8.3 Token-Reuse-Protection

- `consume_token` ist atomar (SELECT FOR UPDATE + UPDATE in derselben Transaktion). Zwei parallele Requests mit demselben Raw-Token: einer redeemed, der andere bekommt None zurück.
- `invalidate_pending_tokens` (in der gleichen Transaktion wie `issue_token`) verhindert dass ein User mehrere aktive Tokens desselben Typs gleichzeitig hat. Re-Send-Welcome invalidiert das alte Welcome-Token; neuer Reset-Request invalidiert den alten Reset-Token.
- Cross-Type-Lookup ist verboten: `consume_token(raw, WELCOME)` matcht nur WELCOME-Tokens. SetPasswordView's `_lookup` ist die einzige Stelle die mehrere Typen akzeptiert (`token_type__in=[WELCOME, RESET]`), das ist by-design.

### 8.4 Self-Re-Send-Welcome ist absichtlich nicht da

Es gibt **keinen** anonymen "Schick mir eine Welcome-Mail"-Endpoint. Ein User der `not has_usable_password()` ist (also ne Welcome-Mail offen hat) muss entweder:
- den existing Link aus seiner Inbox finden (gilt 7 Tage), oder
- den Admin bitten, neu zu senden.

Begründung: ein Anon-Endpoint "send welcome to <email>" wäre eine User-Enumeration-Lücke (gleiche Response-Strategie wie Reset könnte das mitigieren, aber dann gibt's noch das Mail-Volumen-Problem: jemand könnte einen User unbegrenzt mit Welcome-Mails spammen).

Wenn das mal ein Pain-Point wird: separater Spec, mit Captcha o.ä.

### 8.5 Verify-Race: Email-Uniqueness

Zwischen "User klickt Submit auf Email-Change" und "User klickt Verify-Link" kann ein anderer User die Ziel-Email aufnehmen (Admin-Create mit derselben Email, oder ein anderer User ändert seine Email auch dahin):

- `UserCreationForm.clean_email` lehnt es bereits ab wenn die Email schon existiert. → Welcome-Path safe.
- `ProfileIdentityForm` müsste auch `clean_email` haben das Uniqueness prüft. Aber: zwischen `clean_email` und User-klickt-Verify vergehen Sekunden bis Stunden.
- VerifyEmailView prüft im finalen `get()` nochmal: wenn `User.objects.exclude(pk=token_row.user.pk).filter(email__iexact=new_email).exists()` → Error-Message + redirect, ohne Email-Swap.

So bleibt die Race-Window klein und harmlos.

### 8.6 Email-Normalisierung

Wir lowercasen Email **nicht** in der DB-Persistenz — Email wird so gespeichert wie der User getippt hat (`Hans@Example.org` bleibt so). Sämtliche Lookups (`UserCreationForm.clean_email`, `ProfileIdentityForm.clean_email`, `PasswordResetRequestView`, `VerifyEmailView`) verwenden `__iexact` für case-insensitive Matching. Damit kein Datenverlust durch Normalisierung und keine Surprise wenn ein User-Identity-Tool die Original-Schreibweise rückimportiert.

---

## 9. Tests (~28 Tests, 6 Module)

Detailliert, mit Test-Klassen pro Modul:

### 9.1 `tests/test_account_token.py` (~7 Tests)

```
class TestAccountTokenIssueConsume:
    test_issue_returns_raw_and_stores_hash
    test_consume_returns_token_and_marks_used
    test_consume_twice_returns_none_second_call
    test_consume_with_wrong_type_returns_none
    test_consume_expired_returns_none
    test_invalidate_pending_marks_all_unused_of_type
class TestAccountTokenExpiry:
    test_expiry_per_type_matches_constants
```

### 9.2 `tests/test_welcome_flow.py` (~7 Tests)

```
class TestUserCreateWelcomeIntegration:
    test_create_user_sets_unusable_password
    test_create_user_issues_welcome_token
    test_create_user_sends_welcome_mail_to_user_email
    test_create_user_emits_welcome_token_sent_audit
class TestSetPasswordWelcome:
    test_set_password_with_welcome_token_logs_in
    test_set_password_consumes_token
class TestResendWelcome:
    test_admin_resend_invalidates_previous_and_emits_new_audit
```

### 9.3 `tests/test_password_reset_flow.py` (~7 Tests)

```
class TestPasswordResetRequest:
    test_request_existing_email_issues_token_and_sends_mail
    test_request_nonexistent_email_same_response
    test_request_invalidates_previous_unused
class TestPasswordResetRateLimit:
    test_per_user_limit_3_per_hour
    test_per_ip_limit_10_per_hour
class TestPasswordResetConsume:
    test_set_password_with_reset_token_logs_in
    test_set_password_with_reset_token_replaces_password
```

### 9.4 `tests/test_email_verify_flow.py` (~6 Tests)

```
class TestProfileEmailChangeIssuesVerify:
    test_email_change_does_not_mutate_user_email
    test_email_change_issues_verify_token_with_payload
    test_email_change_sends_mail_to_new_address
class TestVerifyEmailClick:
    test_verify_click_swaps_email_and_emits_audit
    test_verify_click_with_consumed_token_fails_gracefully
    test_verify_click_blocked_if_email_taken_in_meantime
```

### 9.5 `tests/test_account_emails.py` (~3 Tests)

```
test_send_welcome_renders_subject_body_and_uses_user_email
test_send_verify_uses_override_to
test_send_email_uses_default_from
```

### 9.6 Bestehende Tests, die angepasst werden müssen

| Test | Anpassung |
|---|---|
| `tests/test_user_update_create_audit.py::test_create_emits_user_created` | POST verliert `password1`/`password2`-Felder. |
| `tests/test_profile_view.py::test_identity_save` | Wenn `email` geändert wird, ist die DB-Email noch alt + pending Mail gegangen. Neuer Test-Pfad für identity-without-email. |
| `tests/test_user_change_form.py::TestUserCreationFormFields` (falls vorhanden) | UserCreationForm hat keine password-Felder mehr. |

### 9.7 Email-Backend in Tests

`django.core.mail.outbox` ist das Standard-Pattern. `settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"` ist Default in pytest-django via `django_test_runner`. Wir asserten:

```python
assert len(mail.outbox) == 1
msg = mail.outbox[0]
assert msg.to == ["m@example.org"]
assert "Welcome" in msg.subject
assert "set-password/" in msg.body
```

---

## 10. Out-of-Scope

Explizit nicht in 2a:

- **HTML-Email-Templates** — Welcome/Reset/Verify bleiben Plain-Text. Ein späterer separater Spec geht über *alle* Email-Sender (inkl. `apps/monitoring/notifications.py`) und stellt sie gemeinsam auf multi-part Plain+HTML um. Der `send_account_email`-Helper ist genau so strukturiert dass diese Umstellung dort nur an einer Stelle passiert.
- **Soft-Delete von Usern** — Sub-Spec 2b. Bis dahin löscht `UserDeleteView` weiterhin hart (mit Audit + SET_NULL-Cascade aus 1c). Das Verhältnis Welcome-Token ⇔ Soft-Delete (was wenn ein soft-deleted User noch einen aktiven Token hat?) wird in 2b geklärt — vorläufig: die Welcome-Tokens haben CASCADE-FK auf User, also löscht ein Hard-Delete sie mit.
- **Member-Approval-Flow** (Applicant beantragt selbst Mitgliedschaft via Self-Service) — separater Spec.
- **i18n der Email-Templates** — bleiben EN. Wenn ein Verein-Mitglied DE-Mails will, ist das ein nachgelagerter Spec der `user.language` respektiert und für jeden Template eine DE-Version mitliefert.
- **Email-Notification-Preferences** (welche Mails will ich kriegen) — separater Spec, sinnvoll erst sobald wir mehr Email-Volumen haben (z.B. Alert-Digests).
- **2FA / TOTP** — irgendwann, nicht jetzt.
- **Cleanup-Job für expired Tokens** — `manage.py prune_account_tokens` als CLI ist sinnvoll, aber bewusst nicht in 2a: kein Cron-Setup im servers-Repo dafür. Da Tokens nach 7 Tagen bzw. 24h ablaufen und das Volumen klein ist (Verein-intern), ist Pruning nicht zeitkritisch. Wenn doch: kleiner Follow-up-PR.
- **Login-with-token (Magic-Link)** — Welcome-Set-Password ist NICHT dasselbe wie ein Magic-Login-Link. Kein "klick und du bist eingeloggt"-Flow ohne Passwort. (Wäre ein separater Spec wenn überhaupt.)
- **Token-Resend via UI für Reset** — heute muss der User die Reset-Page nochmal aufrufen. Ein "Resend"-Button auf dem Reset-Confirmation-Screen wäre Konsistenz mit Welcome, aber bringt wenig — der existing Link kommt schneller per Email als das Resend-Roundtrip.

---

## 11. Migrations + Bitwarden-Secrets

**Eine Django-Migration:** `apps/accounts/migrations/0XXX_account_token.py` — neue Tabelle. Keine Datenmigrationen, keine Bestandsänderungen am User-Modell.

**Bitwarden-Secrets:** Keine neuen Secrets nötig. `EMAIL_HOST_*` sind bereits da (Brevo SMTP für monitoring), `DEFAULT_FROM_EMAIL` auch (`alerts@oe5xrx.org` — wir senden alle Account-Mails von derselben Adresse; für später eine separate `accounts@oe5xrx.org` ist möglich, aber Brevo erlaubt nur whitelisted From-Adressen und der Verein nutzt `alerts@` schon).

`SITE_URL` ist ein neues Setting mit Default `https://remote.oe5xrx.org`. Bitwarden-Eintrag ist OPTIONAL — der Default deckt prod ab. Nur falls man eine Staging-Instanz mit eigener URL aufsetzt, wird der Bitwarden-Override gebraucht. Das Server-Yaml (`servers/services/station_manager/service.yaml`) dokumentiert es im `secrets.optional`-Block.

---

## 12. Risiken + Mitigation

| Risiko | Wahrscheinlichkeit | Impact | Mitigation |
|---|---|---|---|
| Brevo throttled / blockt Mails (z.B. wegen User-Email die ungültig ist) | mittel | User bekommt nie Welcome | Audit-Event `WELCOME_TOKEN_SENT` ist da — Admin sieht "Mail wurde gesendet"; bei Bounce muss er es manuell prüfen. Future: Brevo-Bounce-Webhook ingesten. |
| Email-Provider buffert Verify-Click (Outlook Safe-Links) | mittel | Token wird vom Email-Scanner konsumiert, User-Klick failt | Verify ist GET-only und idempotent in dem Sinn dass der erste Click den Token verwendet — ein zweiter Click zeigt "invalid". Akzeptiert für jetzt; wenn Beschwerden, zwei-Stufen-Verify (GET = Confirm-Page, POST = Verify). |
| `SITE_URL`-Misconfig in dev → Link in Mail zeigt auf `http://remote.oe5xrx.org` statt localhost | hoch in dev, gar nicht in prod | dev-Mails werden eh in Console gedumped (kein echter Send), aber irreführend | `config/settings/dev.py` setzt `SITE_URL = "http://localhost:8000"` explizit. |
| Welcome-Mail im Spam | hoch | User klickt nie Link | Plain-Text + DEFAULT_FROM_EMAIL aus Brevo-verified-Domain — bekannte Anti-Spam-Faktoren. Admin sieht die Email-Adresse im Success-Toast → kann nachfragen wenn User nichts gefunden hat. |
| Race: User-Create mit demselben Username/Email parallel | niedrig | Erste Tx wins, zweite kriegt IntegrityError | UserCreationForm.clean_email + Username-Unique-Constraint. IntegrityError zeigt Generic-Error im UI. |
| Token-Tabelle wächst unbounded | niedrig in 2a | DB-Bloat in Jahren | siehe §10 — pruning ist ein Follow-up; bei kleinem Verein irrelevant. |

---

## 13. Implementierungs-Reihenfolge (für Plan-Phase)

Vorschlag für die spätere Plan-Phase (Sub-Specs werden mit `subagent-driven-development` umgesetzt):

1. **Token-Modell + Helper + Tests** (`test_account_token.py`). Foundation, ohne UI/Email-Coupling.
2. **Email-Helper + Templates + Tests** (`test_account_emails.py`). Standalone.
3. **Welcome-Flow** — UserCreationForm rewrite + UserCreateView audit + Set-Password-View (Welcome-Type) + Re-Send-Welcome + Tests.
4. **Reset-Flow** — Login-Page-Link + PasswordResetRequestView + Set-Password-View (akzeptiert beide Typen) + Rate-Limit + Tests.
5. **Verify-Flow** — ProfileIdentityForm + ProfileView Wiring + VerifyEmailView + Tests.
6. **Cleanup** — alte Tests anpassen, Full-Regression, ruff format, docs/CHANGELOG.

Step 1 + 2 + 3 sind sequenziell. Step 4 + 5 können parallel laufen (Subagents), weil sie verschiedene Form/View-Files berühren.

---

**Spec Owner:** Peter Buchegger
**Letzte Änderung:** 2026-06-13
