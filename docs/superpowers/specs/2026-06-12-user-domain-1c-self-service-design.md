# User-Domain Redesign — Sub-Spec 1c: Self-Service

**Status:** Draft, abgeleitet aus dem Master-Overview am 2026-06-12.
**Bogen:** Dritter Sub-Spec. Folgt dem Overview `2026-06-09-user-domain-redesign-overview.md` und baut auf 1a auf. Kann parallel zu 1b laufen.
**Branch:** `feat/user-domain-1c-self-service` (von `main`, nach Merge von 1a).
**Ziel:** Die Write-Surface des User-Domain bauen. UserUpdateForm bekommt die neuen Profile-Felder. ProfileView wird komplett umgebaut (4 separate Forms: Identity / Profil / Adresse / Passwort). Password-Change-Endpoint mit Re-Auth. Onboarding-Empty-State-Hinweise auf der Profile-Page. UserDeleteView bekommt eine Impact-Anzeige.

Nach Merge dieses Specs kann jeder eingeloggte User sein eigenes Profil komplett pflegen. Admin kann andere User komplett bearbeiten. Die neuen Felder werden mit Geocoding und Avatar-Resize verknüpft.

---

## 1. Kontext

Voraussetzung: **1a ist gemergt.** Damit verfügbar:

- User-Modell mit 10 neuen Feldern.
- `apps/accounts/visibility.py` mit Audience-Modell.
- `apps/accounts/geocoding.py` mit `geocode_address()` + `lat_lon_to_locator()`.
- `apps/accounts/avatars.py` mit `validate_avatar_upload()` + `process_avatar_file()`.
- AccountAuditLog kennt neue EventTypes (USER_CREATED/UPDATED/DELETED/ACTIVATED/DEACTIVATED/PASSWORD_CHANGED + STATION_ASSIGNMENT_*).

Optional Voraussetzung: **1b ist gemergt.** Wenn 1b zuerst läuft, ist UserDetailView bereits da und UserUpdateView ist bereits auf Identity-only reduziert. Wenn 1c **vor** 1b läuft, refactored 1c die Cards aus UserUpdateView nicht (das ist 1b's Job) — der Edit-Form bekommt nur die neuen Felder.

**Empfehlung: 1b zuerst implementieren, dann 1c.** Damit hat der Self-Edit-Redirect (auf UserDetailView) ein Ziel.

Wenn parallel: kein Konflikt im Code (verschiedene Files), aber 1c.success_url muss als TBD markiert werden falls UserDetailView noch nicht existiert.

Dieser Sub-Spec ergänzt:

- UserChangeForm + UserCreationForm + ProfileForm mit den neuen Feldern.
- ProfileView komplett umgebaut: 4 Forms statt einem, eigene POST-URLs.
- ProfilePasswordChangeView neu (Self-Service Password-Change).
- Onboarding-Empty-State-Hinweise auf der Profile-Page.
- UserDeleteView Impact-Anzeige.
- USER_*-Audit-Emissionen in den `form_valid`-Methoden.
- Geocoding-Trigger im `form_valid` wenn Address ändert.
- Avatar-Resize im Form.save().

---

## 2. Schema-Anbindung (keine Migration)

Keine neuen Felder — 1a hat alle Felder bereits geliefert. Dieser Sub-Spec bindet sie nur an Forms und Templates.

---

## 3. Edit-Form (UserChangeForm + UserCreationForm)

### 3.1 UserChangeForm — neue Felder

```python
# apps/accounts/forms.py

class UserChangeForm(BaseUserChangeForm):
    password = None

    class Meta:
        model = User
        fields = (
            "username", "email", "first_name", "last_name", "language",
            "is_active",
            "bio", "avatar", "qth_name", "qrz_url", "phone",
            "address", "locator",
            "is_directory_visible",
        )
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
            "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "bio": forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": 500}),
            "avatar": forms.ClearableFileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(attrs={
                "class": "form-control", "placeholder": "JN78AB",
            }),
            "is_directory_visible": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_avatar(self):
        from .avatars import validate_avatar_upload
        f = self.cleaned_data.get("avatar")
        validate_avatar_upload(f)
        return f

    def clean_locator(self):
        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise ValidationError(
                _("Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein (z.B. JN78AB).")
            )
        return loc

    def save(self, commit=True):
        user = super().save(commit=commit)
        # Avatar-Resize-Pipeline nach Save (File ist auf Disk)
        if commit and "avatar" in self.changed_data and user.avatar:
            from .avatars import process_avatar_file
            process_avatar_file(user.avatar.path)
        return user
```

### 3.2 UserCreationForm — bleibt schlank

UserCreationForm bekommt **keine** neuen Felder. Admin gibt nur Identity-Felder ein, neue Profile-Felder füllt der User selbst über `ProfileView`, oder Admin nachträglich über UserUpdateView.

```python
class UserCreationForm(BaseUserCreationForm):
    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name", "language")
        # password1/password2 kommen aus BaseUserCreationForm
```

Roadmap: in Spec #2 (Account Lifecycle) wird UserCreationForm umgebaut auf Welcome-Email-Setup-Flow. Bis dahin bleibt der bestehende Mit-Password-Flow.

### 3.3 ProfileForm — Self-Edit

ProfileForm ist nicht ein einzelnes Form, sondern **drei** Forms je Panel auf der Profile-Page. Plus ein PasswordChangeForm. Details in Sektion 4.

```python
class ProfileIdentityForm(forms.ModelForm):
    """Self-Edit für Identity-Felder."""
    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "language")
        widgets = {
            "email": forms.EmailInput(attrs={"class": "form-control"}),
            "first_name": forms.TextInput(attrs={"class": "form-control"}),
            "last_name": forms.TextInput(attrs={"class": "form-control"}),
            "language": forms.Select(attrs={"class": "form-select"}),
        }


class ProfileProfileForm(forms.ModelForm):
    """Self-Edit für die Profil-Kosmetik-Felder (avatar/bio/qth/qrz/phone)."""
    class Meta:
        model = User
        fields = ("avatar", "bio", "qth_name", "qrz_url", "phone", "is_directory_visible")
        widgets = {
            "avatar": forms.ClearableFileInput(attrs={"class": "form-control", "accept": "image/*"}),
            "bio": forms.Textarea(attrs={"class": "form-control", "rows": 3, "maxlength": 500}),
            "qth_name": forms.TextInput(attrs={"class": "form-control"}),
            "qrz_url": forms.URLInput(attrs={"class": "form-control"}),
            "phone": forms.TextInput(attrs={"class": "form-control"}),
            "is_directory_visible": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }

    def clean_avatar(self):
        from .avatars import validate_avatar_upload
        f = self.cleaned_data.get("avatar")
        validate_avatar_upload(f)
        return f

    def save(self, commit=True):
        user = super().save(commit=commit)
        if commit and "avatar" in self.changed_data and user.avatar:
            from .avatars import process_avatar_file
            process_avatar_file(user.avatar.path)
        return user


class ProfileAddressForm(forms.ModelForm):
    """Self-Edit für Address + Locator. Geocoding-Trigger im View."""
    class Meta:
        model = User
        fields = ("address", "locator")
        widgets = {
            "address": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
            "locator": forms.TextInput(attrs={
                "class": "form-control", "placeholder": "JN78AB",
            }),
        }

    def clean_locator(self):
        loc = self.cleaned_data.get("locator", "").strip().upper()
        if loc and not LOCATOR_REGEX.match(loc):
            raise ValidationError(_("Locator muss 2 Buchstaben + 2 Ziffern + 2 Buchstaben sein."))
        return loc


class PasswordChangeForm(DjangoPasswordChangeForm):
    """Bootstrap-styled overlay über Django's PasswordChangeForm."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs["class"] = "form-control"
```

### 3.4 Edit-Form-Layout (user_form.html)

```
page-head:
├ breadcrumb: Users > {username} > Edit  (oder: Users > Create)
├ page-title: form_title
└ (kein Page-Head-Actions im Edit-Mode)

grid grid-main:
├ Linke Spalte (Form, mehrere Panels):
│   │
│   ├ Panel "Identity":
│   │   ├ form-row: [username] [is_active checkbox]
│   │   ├ form-row: [first_name] [last_name]
│   │   ├ form-row: [email]
│   │   ├ form-row: [language] [is_directory_visible checkbox]
│   │   └ (Create-Mode) form-row: [password1] [password2]
│   │
│   ├ Panel "Profil" (nur Edit-Mode):
│   │   ├ form-row: [avatar (FileInput)]
│   │   ├ form-row: [bio (Textarea)]
│   │   ├ form-row: [qth_name] [qrz_url]
│   │   └ form-row: [phone]
│   │
│   └ Panel "Adresse & Standort" (nur Edit-Mode):
│       ├ form-row: [address (Textarea)]
│       ├ Hinweis-Text: "Locator + lat/lon werden bei Speichern aus der Adresse berechnet."
│       └ form-row: [locator (manueller Override)]
│
└ Rechte Spalte (aside, panel):
    ├ Im Edit-Mode: dlist mit pk, date_joined, last_login, current membership-level,
    │                lat/lon (Debug-Anzeige), is_directory_visible-State
    └ Im Create-Mode: Info-Box "Profil-Daten ergänzt der User selbst über sein Profil."

panel-foot:
├ [Save user]
└ [Cancel] → Detail (Edit) bzw. Liste (Create)
```

Inline `style="max-width:640px"` wird komplett entfernt. Das `grid grid-main` collapsed bei `≤ 1024px` automatisch.

---

## 4. ProfileView — Komplett-Umbau

Heute hat `ProfileView` ein einziges Form mit 4 Feldern (email, first_name, last_name, language). Neu:

### 4.1 ProfileView dispatchet vier Forms

Ein einziger View hat vier Forms unter sich. Im POST diskriminiert er per Form-Identifier-Hidden-Field, welches Form abgeschickt wurde.

```python
# apps/accounts/views.py

class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"
    success_url = reverse_lazy("accounts:profile")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["identity_form"] = ProfileIdentityForm(instance=user, prefix="identity")
        ctx["profile_form"] = ProfileProfileForm(instance=user, prefix="profile")
        ctx["address_form"] = ProfileAddressForm(instance=user, prefix="address")
        ctx["password_form"] = PasswordChangeForm(user=user)
        # Onboarding-Hint-Trigger:
        ctx["onboarding_hints"] = self._onboarding_hints(user)
        # Eigene Sessions:
        from apps.sso.views import _active_sessions_for
        ctx["self_sessions"] = _active_sessions_for(user)
        return ctx

    def post(self, request, *args, **kwargs):
        form_name = request.POST.get("form_name", "")
        user = request.user
        if form_name == "identity":
            return self._save_identity(request, user)
        if form_name == "profile":
            return self._save_profile(request, user)
        if form_name == "address":
            return self._save_address(request, user)
        # Password change goes to its own URL — siehe Sektion 4.4.
        messages.error(request, _("Unknown form."))
        return redirect(self.success_url)

    def _save_identity(self, request, user):
        form = ProfileIdentityForm(request.POST, instance=user, prefix="identity")
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Identity updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect(self.success_url)

    def _save_profile(self, request, user):
        form = ProfileProfileForm(request.POST, request.FILES, instance=user, prefix="profile")
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Profile updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect(self.success_url)

    def _save_address(self, request, user):
        form = ProfileAddressForm(request.POST, instance=user, prefix="address")
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._maybe_geocode(user, changed)
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Address updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect(self.success_url)

    def _maybe_geocode(self, user, changed_fields):
        if "address" not in changed_fields:
            return
        from .geocoding import geocode_address, lat_lon_to_locator
        if not user.address:
            user.latitude = None
            user.longitude = None
            if "locator" not in changed_fields:
                user.locator = ""
            user.save(update_fields=["latitude", "longitude", "locator"])
            return
        coords = geocode_address(user.address)
        if coords:
            lat, lon = coords
            user.latitude = lat
            user.longitude = lon
            if not user.locator or "locator" not in changed_fields:
                user.locator = lat_lon_to_locator(float(lat), float(lon))
            user.save(update_fields=["latitude", "longitude", "locator"])

    def _emit_user_updated(self, request, user, changed_fields):
        tracked = changed_fields & TRACKED_USER_FIELDS
        if tracked:
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_UPDATED,
                actor=request.user,
                target_user=user,
                message=f"self-edit changed: {', '.join(sorted(tracked))}",
                ip_address=_get_client_ip(request),
            )

    def _onboarding_hints(self, user):
        """Returnt Dict von Hint-Key zu Bool: which Empty-State-Hints zu rendern sind."""
        return {
            "name_missing": not (user.first_name or user.last_name),
            "avatar_missing": not user.avatar,
            "bio_missing": not user.bio,
            "qth_missing": not user.qth_name,
            "address_missing": not user.address,
        }
```

### 4.2 Layout — accounts/profile.html

```
page-head:
├ page-eyebrow: "Your account"
├ page-title: "Profile"
└ page-sub: "Verwalte deine Identität, Profil, Kontaktdaten und Standort."

grid grid-main:
├ Linke Spalte (vier Forms):
│   ├ Panel "Identity"  → <form action="" method="post">
│   │     <input type="hidden" name="form_name" value="identity">
│   │     {{ identity_form }} ... [Save identity]
│   │
│   ├ Panel "Profil"  → <form action="" method="post" enctype="multipart/form-data">
│   │     <input type="hidden" name="form_name" value="profile">
│   │     Felder: avatar, bio, qth_name, qrz_url, phone, is_directory_visible
│   │     is_directory_visible mit Erklär-Text:
│   │       "Wenn aus, sehen andere Mitglieder nur Callsign + Rolle + Avatar."
│   │     {{ profile_form }} ... [Save profile]
│   │     (Onboarding-Hints im body, vor [Save])
│   │
│   ├ Panel "Adresse & Standort"  → <form action="" method="post">
│   │     <input type="hidden" name="form_name" value="address">
│   │     Felder: address, locator
│   │     {{ address_form }} ... [Save address]
│   │     (Onboarding-Hint für Adresse oben)
│   │
│   └ Panel "Passwort ändern"  → <form action="{% url 'accounts:password_change' %}" method="post">
│         {{ password_form }} ... [Change password]
│
└ Rechte Spalte (aside):
    ├ Identity-dlist: Callsign (username — readonly), Membership-Pill,
    │                 last_login, date_joined
    └ Panel "Eigene Sessions": Mini-Übersicht aktiver SSO-Sessions mit Revoke
        (entfaltet sich aus _sessions_card.html mit readonly_self=True)
```

### 4.3 Onboarding-Empty-State

Pro Panel-Body wird der Hint conditional gerendert:

```django
{# In Profil-Panel #}
{% if onboarding_hints.avatar_missing %}
  <div class="onboarding-hint" role="note">
    <span class="onboarding-hint-icon">📷</span>
    <span class="onboarding-hint-text">
      {% trans "Lade ein Profilbild hoch, damit dich andere Mitglieder im Verzeichnis erkennen." %}
    </span>
  </div>
{% endif %}
{% if onboarding_hints.bio_missing %}
  <div class="onboarding-hint" role="note">
    <span class="onboarding-hint-icon">✍️</span>
    <span class="onboarding-hint-text">
      {% trans "Stell dich kurz vor (max. 500 Zeichen)." %}
    </span>
  </div>
{% endif %}
{% if onboarding_hints.qth_missing %}
  <div class="onboarding-hint" role="note">
    <span class="onboarding-hint-icon">📍</span>
    <span class="onboarding-hint-text">
      {% trans "QTH-Name? Das ist dein Funker-Standort-Label." %}
    </span>
  </div>
{% endif %}
```

Mapping:

| Panel | Trigger | Text |
|---|---|---|
| Identity | `name_missing` | „Trag deinen Real-Namen ein — andere Mitglieder sehen ihn im Verzeichnis." |
| Profil | `avatar_missing` | „Lade ein Profilbild hoch." |
| Profil | `bio_missing` | „Stell dich kurz vor (max. 500 Zeichen)." |
| Profil | `qth_missing` | „QTH-Name? Das ist dein Funker-Standort-Label." |
| Adresse | `address_missing` | „Trag deine Adresse ein — Locator und lat/lon werden automatisch berechnet." |

Visual: dezent — kleine `border-left:3px solid var(--accent-soft)` Box mit Icon + 1 Satz. CSS-Klasse `onboarding-hint` neu in `app.css`.

Hints erscheinen nur auf der Profile-Page, nicht auf der Detail-Page.

### 4.4 ProfilePasswordChangeView

```python
class ProfilePasswordChangeView(LoginRequiredMixin, View):
    """Self-only password change endpoint, posted from the Profile page."""

    def post(self, request):
        from django.contrib.auth import update_session_auth_hash
        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED,
                actor=request.user,
                target_user=request.user,
                message="self-edit changed: password",
                ip_address=_get_client_ip(request),
            )
            messages.success(request, _("Password updated successfully."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")
```

`update_session_auth_hash` ist wichtig: ohne den Call killt der Password-Change die laufende Session.

URL: `accounts/profile/password/` → `ProfilePasswordChangeView` (POST-only).

Audit: PASSWORD_CHANGED, Message konstant — kein Passwort-Wert geleakt.

---

## 5. UserUpdateView — `form_valid` mit Audit-Emission

```python
TRACKED_USER_FIELDS = {
    "username", "email", "first_name", "last_name", "language",
    "bio", "avatar", "qth_name", "qrz_url", "phone",
    "address", "locator", "is_directory_visible",
}


class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserChangeForm

    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        changed_fields = set(form.changed_data)
        response = super().form_valid(form)

        # Geocoding-Trigger (synchron)
        self._maybe_geocode(self.object, changed_fields)

        # Identity-Diff
        tracked = changed_fields & TRACKED_USER_FIELDS
        if tracked:
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_UPDATED,
                actor=self.request.user,
                target_user=self.object,
                message=f"changed: {', '.join(sorted(tracked))}",
                ip_address=_get_client_ip(self.request),
            )
        # is_active-Flip
        if "is_active" in changed_fields:
            event = (
                AccountAuditLog.EventType.USER_ACTIVATED if self.object.is_active
                else AccountAuditLog.EventType.USER_DEACTIVATED
            )
            AccountAuditLog.log(
                event_type=event,
                actor=self.request.user,
                target_user=self.object,
                message="",
                ip_address=_get_client_ip(self.request),
            )
        return response

    def _maybe_geocode(self, user, changed_fields):
        # Selbe Logik wie ProfileView._maybe_geocode — kann gerne in einen
        # geteilten Helper in apps/accounts/views_helpers.py extrahiert werden.
        ...
```

Wenn nur `is_active` flippt aber kein anderes Feld ändert, gibt es nur `USER_ACTIVATED/DEACTIVATED`, kein `USER_UPDATED`. Feed bleibt pro Ereignis-Typ sauber.

---

## 6. UserCreateView — `form_valid`

```python
class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserCreationForm

    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        response = super().form_valid(form)
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_CREATED,
            actor=self.request.user,
            target_user=self.object,
            message=f"{self.object.username} <{self.object.email}>",
            ip_address=_get_client_ip(self.request),
        )
        return response
```

---

## 7. UserDeleteView — Impact-Anzeige + Audit-Emit

### 7.1 Template

```
page-head:
├ t-danger "Delete user"
└ page-sub: "Delete user {username}? This cannot be undone."

panel (danger border):
├ panel-body:
│   ├ Identity-dlist: Username, Email, Role, Joined
│   ├ Trennlinie
│   ├ "Mit dem User werden gelöscht:" + dlist:
│   │   ├ Station-Assignments: {n}    "werden revoked, Audit emitted"
│   │   ├ Region-Assignments:  {n}    "werden revoked, Audit emitted"
│   │   ├ SSO Grants:          {n}    "werden revoked, Tokens invalidated"
│   │   ├ Active SSO Sessions: {n}    "werden terminated"
│   │   └ Group Memberships:   {n}    "werden entfernt"
│   └ Wenn User auf irgendeiner Station Admin ist:
│       Warnung mit Liste der betroffenen Stations.
│
└ panel-foot:
    ├ [Delete user] (data-confirm)
    └ [Cancel] → users:user_detail
```

### 7.2 View

```python
class UserDeleteView(AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("accounts:user_list")
    context_object_name = "target_user"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.object
        ctx["n_station_assignments"] = user.station_assignments.count()
        ctx["n_region_assignments"] = user.region_assignments.count()
        ctx["station_admin_assignments"] = list(
            user.station_assignments.filter(role="admin").select_related("station")
        )
        # SSO-Counts:
        ctx["n_sso_grants"] = user.app_grants.filter(active=True).count() if hasattr(user, "app_grants") else 0
        ctx["n_active_sessions"] = (
            user.token_sessions.filter(revoked_at__isnull=True).count()
            if hasattr(user, "token_sessions") else 0
        )
        ctx["n_group_memberships"] = user.groups.count()
        return ctx

    def form_valid(self, form):
        if self.get_object() == self.request.user:
            messages.error(self.request, _("You cannot delete your own account."))
            return redirect(self.success_url)
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_DELETED,
            actor=self.request.user,
            target_user=self.object,  # noch da
            message=f"{self.object.username} <{self.object.email}>",
            ip_address=_get_client_ip(self.request),
        )
        messages.success(self.request, _("User deleted successfully."))
        return super().form_valid(form)
```

USER_DELETED wird **vor** `super().form_valid` emittiert, sonst ist `target_user` durch SET_NULL nach Cascade NULL — Username steht aber in `message` so oder so.

---

## 8. Mobile-Spezifika

Während dieses Sub-Specs werden Templates mobile-tauglich gemacht:

- **Raus**: inline `style="max-width:520px"` in user_confirm_delete.html, inline `style="max-width:640px"` in user_form.html und profile.html.
- **Rein**: `grid grid-main`, `form-row` für Field-Pairs, `form-control` Touch-Target ≥ 44px.
- **CSS-Klasse `onboarding-hint`** neu in app.css (dezenter Border-Left, Mobile-Padding).
- **Form-Panels** schalten mobil auf single-column.
- **Avatar-Upload-Picker** auf Mobile mit `accept="image/*"` und max-Resolution-Hinweis im Helper-Text.

UI-Edits über `pixel`-Subagent mit `Skill("frontend-design")`.

---

## 9. Testing

### 9.1 Edit-Form (Admin)

`apps/accounts/tests/test_user_edit_form.py` (neu):

- UserChangeForm enthält alle 13 erwarteten Felder.
- `clean_locator` lehnt `XX` ab, akzeptiert `JN78AB`.
- `clean_avatar` lehnt 3MB-Datei ab.
- Form.save() ruft `process_avatar_file` für neu hochgeladenes Avatar.
- UserCreationForm bleibt schlank (5 Felder + password1/2).
- UserUpdateView.get_success_url führt zur DetailView.

### 9.2 ProfileView

`apps/accounts/tests/test_profile_view.py` (neu):

- GET `/accounts/profile/` rendert 4 Forms + sidebar.
- POST mit `form_name=identity` saved nur Identity-Felder.
- POST mit `form_name=profile` saved Avatar + Bio + QTH + QRZ-URL + Phone + is_directory_visible.
- POST mit `form_name=address` saved Adresse + Locator, ruft `_maybe_geocode`.
- POST mit invalidem Form bringt messages.error.
- `_emit_user_updated` schreibt USER_UPDATED-Audit mit `self-edit changed:` Prefix.
- Onboarding-Hints werden im Context geliefert pro leerem Feld.

### 9.3 Password-Change

`apps/accounts/tests/test_password_change.py` (neu):

- POST `/accounts/profile/password/` mit gültigem current_password + matching new1/new2 → 302 redirect + Session bleibt → PASSWORD_CHANGED-Audit-Eintrag.
- Falsches current_password → Form-Error in messages, kein Audit.
- new1 != new2 → Form-Error, kein Audit.
- Password gegen `AUTH_PASSWORD_VALIDATORS` → Form-Error.
- Audit-Message ist konstant `"self-edit changed: password"`.
- Login mit altem PW schlägt nach Change fehl.

### 9.4 Geocoding-Integration

`apps/accounts/tests/test_profile_geocoding.py` (neu):

- ProfileAddressForm-Save mit neuer Adresse → `geocode_address`-Mock returnt (lat, lon) → User-Felder gesetzt + locator berechnet.
- Address geleert → lat/lon/locator gelöscht (außer locator wurde manuell gesetzt).
- Geocoding-Failure → lat/lon bleiben, locator bleibt.
- UserUpdateView gleicher Geocoding-Trigger.

### 9.5 UserDeleteView

`apps/accounts/tests/test_user_delete_view.py` (neu):

- Confirm-Page zeigt Counts.
- Confirm-Page zeigt Station-Admin-Warnung wenn anwendbar.
- USER_DELETED-Audit emittiert vor Cascade.
- Self-Delete-Versuch → messages.error + Redirect ohne Delete.
- Cascade entfernt Station/Region-Assignments.

### 9.6 Onboarding-Empty-State

`apps/accounts/tests/test_profile_onboarding.py` (neu):

- User mit allen Profilfeldern leer → alle 5 Onboarding-Hints im HTML.
- User mit Avatar gesetzt → Avatar-Hint fehlt, andere noch da.
- User mit allen Feldern gefüllt → keine Hints im HTML.
- Hints erscheinen nur auf `accounts:profile`, nicht auf `accounts:user_detail`.

### 9.7 Mobile-Snapshot (optional)

Falls Playwright im Projekt vorhanden ist: Edit-, Create-, Delete-, Profile-Page bei Viewports 375 / 768 / 1024 px.

---

## 10. Implementation-Reihenfolge

**Round-1 — Build**:

1. **Phase 1: Edit-Form-Erweiterung + Audit-Emission + Geocoding-Trigger** (Backend + Template).
   Subagents: `gateway` (Backend) + `pixel` mit frontend-design (Template).
   - UserChangeForm + UserCreationForm extended.
   - UserUpdateView.form_valid mit Audit-Emit + Geocoding-Trigger.
   - UserCreateView.form_valid mit USER_CREATED-Emit.
   - user_form.html Mobile-Refactor + neue Panels.
   - Dauer: ~3-4h.

2. **Phase 2: ProfileView komplett-Umbau** (Backend + Template).
   Subagents: `gateway` + `pixel` mit frontend-design.
   - ProfileIdentity/Profile/Address-Forms.
   - ProfileView mit form_name-Dispatch.
   - profile.html mit 4 Panels.
   - Onboarding-Hints im Context.
   - Dauer: ~4-5h.

3. **Phase 3: Password-Change-Endpoint** (Backend + Template).
   Subagents: `gateway` + `pixel` mit frontend-design.
   - PasswordChangeForm-Klasse.
   - ProfilePasswordChangeView + URL.
   - Password-Panel auf profile.html.
   - PASSWORD_CHANGED-EventType-Emit.
   - Dauer: ~2h.

4. **Phase 4: UserDeleteView Impact-Anzeige** (Backend + Template).
   Subagents: `gateway` + `pixel` mit frontend-design.
   - get_context_data mit Counts + Station-Admin-Liste.
   - user_confirm_delete.html mit Impact-Panel.
   - USER_DELETED-Emit.
   - Dauer: ~2h.

**Round-1.5: code-simplifier**.

**Round-2 — Watcher**:

- `audit` auf Form-Code: Validator-Pattern, save()-Side-Effects, kein Code-Duplication.
- `guard` auf Password-Change: Re-Auth, Session-Hash-Update, kein Password-Log-Leak.
- `vault` auf Geocoding-Trigger: Transaction-Safety, idempotente Edge-Cases.
- `probe` auf E2E: alle Flows funktionieren, bestehende Tests grün.

**Round-2.5: probe** Test-Writer.

**Round-3 — `pr-review-toolkit:review-pr`**.

---

## 11. Out-of-Scope (für 1c)

- Welcome-Email-Flow / Setup-Token → Spec #2 (UserCreateView wird dort umgebaut).
- Soft-Delete → Spec #2 (UserDeleteView wird dort zur Archive-Surface).
- Password-Reset (forgot-password) → Spec #2.
- Email-Change-Verification → Spec #2.
- Member-Directory (Detail/List) → 1b.
- USER_*-Audit-Emissionen für STATION_ASSIGNMENT_* (das machen Signals aus 1a).

---

## 12. Offene Punkte

Keine. Alle Entscheidungen sind im Master-Overview (Sektion 8) festgehalten.
