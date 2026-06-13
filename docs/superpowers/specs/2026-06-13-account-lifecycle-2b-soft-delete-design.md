# Account Lifecycle — Sub-Spec 2b: Soft-Delete

**Status:** Draft, brainstormed 2026-06-13.
**Bogen:** Zweiter Sub-Spec des Account-Lifecycle-Arcs (nach 2a Token-Email-Flows). Schließt den User-Domain-Arc ab — danach hat OE5XRX alle Lifecycle-Bausteine für Mitglieder (Welcome → Self-Service → Reset → Delete → Restore → Purge).
**Branch:** `feat/account-lifecycle-2b-soft-delete` (von `main`, nach Merge von 2a).
**Ziel:** Den heute harten `UserDeleteView` durch einen zweistufigen Lifecycle ersetzen — **Soft-Delete** (reversibel, Tombstone bleibt für Audit/History) und **Hard-Purge** (irreversibel, nur nach erfolgreichem Soft-Delete erreichbar). Topologie-Zuweisungen werden beim Soft-Delete auto-revoked und der Admin sieht im Success-Banner welche Positionen frei wurden. Alle User-Action-Buttons (Edit, Soft-Delete, Restore, Hard-Purge) wandern auf `UserDetailView`; die Liste wird zur reinen Browse-Surface mit Filter-Bar.

Nach Merge dieses Specs ist der User-Domain-Arc komplett: 1a Foundation + 1b Directory + 1c Self-Service + 2a Email-Flows + 2b Soft-Delete.

---

## 1. Kontext

Voraussetzungen sind alle auf `main`:

- **User-Domain 1a/1b/1c** sind komplett — Profile-Felder, Visibility-Modell, Audit-Layer mit `USER_CREATED/UPDATED/DELETED/…`-Events, UserDeleteView mit Cascade-Impact-Anzeige.
- **2a Token-Email-Flows** ist live — `AccountToken`-Modell mit `welcome / reset / verify`, `account_tokens`-Helper, ProfileView-Email-Verify-Path. Tokens haben `on_delete=CASCADE` auf User.
- **#74 LoginRequiredMiddleware** ist aktiv — jede neue View ist by-default login-required, anonyme Endpoints brauchen `@method_decorator(login_not_required, name="dispatch")`.
- **Topology-Audit** aus 1a ist da — `STATION_ASSIGNMENT_REVOKED` und `REGION_ASSIGNMENT_REVOKED`-Events werden bereits emittiert beim manuellen Revoke. Wir wiederverwenden sie für den Auto-Revoke-Pfad beim Soft-Delete.
- **SSO-Sessions + AppGrants** aus dem SSO-Stack haben `revoked_at`/`revoked_by` und Signal-Cascade beim User-Hard-Delete. 2b nutzt dieselben Felder über den existing `_revoke_sso`-Pfad, aber explizit beim Soft-Delete (das emittiert kein automatisches User-Delete-Signal).

Was heute fehlt und 2b adressiert:

- **Hard-Delete ist die einzige Option** — ein versehentlicher Klick im Confirm-Dialog ist destruktiv. Audit-Rows mit `target_user`-FK auf den User werden zu Tombstones mit SET_NULL; die `message`-Strings (`f"{username} <{email}>"`) sind die einzige menschenlesbare Spur.
- **Kein Restore** — wenn ein Verein-Mitglied versehentlich gelöscht wird oder seinen Austritt zurücknimmt, ist die Arbeit (Topologie, Membership-Level, Profil-Daten) weg.
- **Username- und Email-Reuse** sind blockiert durch implizite unique-Constraints und durch `__iexact`-Forms-Checks — kein neuer User kann den Callsign eines ausgetretenen Mitglieds übernehmen, was Vereinsrealität widerspricht.
- **Topologie-Cascade** beim Hard-Delete passiert silent — wenn ein Station-Admin gelöscht wird, verliert die Station ihren Admin ohne Audit-Spur (nur User-Delete-Audit, kein per-Assignment-Audit).

**Out of Scope (siehe §10):**
- Anonymisierung / PII-Scrubbing beim Soft-Delete.
- Auto-Purge nach Retention-Frist.
- Self-Delete (User kann sich nicht selbst soft-deleten — Austritte über den Vereinsvorstand).
- Email-Benachrichtigung an Vereins-Admins bei frei gewordenen Positionen.

---

## 2. Datenmodell

### 2.1 User-Modell — zwei neue Felder + Conditional Username-Unique

```python
# in apps/accounts/models.py User class
deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)
deleted_by = models.ForeignKey(
    "self", null=True, blank=True, on_delete=models.SET_NULL,
    related_name="deleted_users",
)

class Meta:
    constraints = [
        # Username-Uniqueness nur für nicht-soft-deleted User —
        # erlaubt Callsign-Reuse nach Soft-Delete.
        models.UniqueConstraint(
            fields=["username"],
            condition=Q(deleted_at__isnull=True),
            name="unique_active_username",
        ),
    ]
```

`deleted_at IS NOT NULL` = "user ist soft-deleted". `deleted_by` zeigt auf den Admin der den Soft-Delete ausgelöst hat (`SET_NULL` falls dieser Admin später selbst gelöscht wird — Tombstone-Pattern).

**Email kriegt kein DB-Level-Unique-Constraint** (hatte heute auch keinen) — Uniqueness wird auf Forms-Layer via `__iexact`-Lookup gemacht, der nicht-deleted User filtert.

### 2.2 Migration

Single migration `0XXX_user_soft_delete.py` mit zwei Operations:

1. **AddField**: `deleted_at` (DateTimeField nullable, db_index) + `deleted_by` (FK self, SET_NULL).
2. **RemoveConstraint** (implicit unique auf `username` aus `AbstractUser`) + **AddConstraint** (`unique_active_username` mit `condition=Q(deleted_at__isnull=True)`).

Step 2 ist Postgres-spezifisch:
```sql
-- conceptually what Django emits:
DROP INDEX accounts_user_username_key;
CREATE UNIQUE INDEX unique_active_username
    ON accounts_user (username)
    WHERE deleted_at IS NULL;
```

Keine Datenmigrationen — alle existing User haben `deleted_at=NULL`, sind also "aktiv" mit dem neuen Schema.

### 2.3 UserManager-Helper

```python
class UserManager(BaseUserManager):
    # ... existing methods (create_user, create_superuser) bleiben ...

    def active(self):
        """Convenience: User.objects.active() → non-soft-deleted."""
        return self.filter(deleted_at__isnull=True)

    def deleted(self):
        """Convenience: User.objects.deleted() → soft-deleted only."""
        return self.filter(deleted_at__isnull=False)
```

**Wichtig: kein default-Filter.** `User.objects.all()` zeigt weiterhin alles. Default-Manager-Filtering wäre invasiv und würde Audit/Sessions/SSO subtle brechen — wir filtern stattdessen explizit pro Use-Case (siehe §3.1).

---

## 3. Queries — wo gefiltert wird

### 3.1 Filter-Map

| Stelle | Query |
|---|---|
| `UserListView` (default, `?show=active`) | `User.objects.filter(deleted_at__isnull=True, is_active=True)` |
| `UserListView` (`?show=inactive`) | `User.objects.filter(deleted_at__isnull=True, is_active=False)` |
| `UserListView` (`?show=deleted`) | `User.objects.filter(deleted_at__isnull=False)` |
| `UserListView` (`?show=all`) | `User.objects.all()` |
| `UserDetailView` | `User.objects.all()` — rendert sowohl active als auch deleted; UI-Buttons kondional |
| `UserCreationForm.clean_email` | `User.objects.active().filter(email__iexact=email).exists()` |
| `UserCreationForm.clean_username` | redundant zum DB-Constraint, aber für besseres Form-Error-Message: `User.objects.active().filter(username__iexact=username).exists()` |
| `ProfileIdentityForm.clean_email` | `User.objects.active().exclude(pk=self.instance.pk).filter(email__iexact=email).exists()` |
| `apps/accounts/visibility.py` (1b — `user_can_view_directory`, audience-Filter) | `User.objects.active()`-Filter wird in die existing audience-Logik integriert |
| `apps/monitoring/recipients.py` (1a — `recipients_for_station_alert`) | Filter wird ergänzt um `deleted_at__isnull=True` |
| Auth-Backend (`ModelBackend.user_can_authenticate`) | Django checkt `is_active`; soft-deleted hat `is_active=False` → automatisch gesperrt. Keine Code-Änderung nötig. |
| `LoginView` Lookup | `username` ist im UNIQUE-Constraint conditional auf `deleted_at__isnull=True` → ein zweiter User mit gleichem Username (z.B. recycled-Callsign) ist erlaubt; der DB-Index findet den aktiven beim Login. |
| `_active_sessions_for(user)` (SSO-Sessions-Karte) | sieht den deleted User immer noch (Detail-Page rendert ihn) — keine Änderung |
| AuditLog-actor/target FK-Resolver | ungefiltert — Audit-Page muss soft-deleted User rendern können |

### 3.2 Was passiert mit dem Login eines soft-deleted Users

Drei Layer schützen unabhängig:

1. **Authentication-Backend** (`ModelBackend.user_can_authenticate`) returnt `False` bei `is_active=False`. Soft-Delete setzt `is_active=False`, der User ist also out-of-the-box gesperrt.
2. **Conditional UNIQUE-Index** auf username: wenn nach Soft-Delete ein neuer User mit demselben Callsign angelegt wird, gibt's keine Login-Ambiguität — der DB-Constraint stellt sicher dass es zu jedem Zeitpunkt nur einen aktiven User mit diesem Username gibt.
3. **2a-Token-Invalidierung** beim Soft-Delete (siehe §4.1): alle pending Welcome/Reset/Verify-Tokens werden invalidiert. Ein soft-deleted User kann nicht über einen alten Reset-Link einen neuen Login etablieren.

---

## 4. Soft-Delete-View

### 4.1 `UserSoftDeleteView`

Ersetzt das heutige `UserDeleteView`. URL-Name wird von `user_delete` zu `user_soft_delete` umbenannt; Templates die `{% url 'accounts:user_delete' %}` referenzieren, werden mit-aktualisiert.

```python
class UserSoftDeleteView(AdminRequiredMixin, View):
    template_name = "accounts/user_confirm_soft_delete.html"

    def get_object(self):
        # 404 wenn schon soft-deleted — Re-Soft-Delete nicht möglich.
        return get_object_or_404(
            User, pk=self.kwargs["pk"], deleted_at__isnull=True,
        )

    def get(self, request, pk):
        target = self.get_object()
        ctx = {
            "target_user": target,
            "n_station_assignments": target.station_assignments.count(),
            "n_region_assignments": target.region_assignments.count(),
            "station_admin_assignments": list(
                target.station_assignments
                      .filter(role=StationAssignment.Role.ADMIN)
                      .select_related("station")
            ),
            "n_sso_grants": (
                target.app_grants.count() if hasattr(target, "app_grants") else 0
            ),
            "n_active_sessions": (
                target.token_sessions.filter(revoked_at__isnull=True).count()
                if hasattr(target, "token_sessions") else 0
            ),
            "n_group_memberships": target.groups.count(),
            "n_pending_tokens": target.account_tokens.filter(used_at__isnull=True).count(),
        }
        return render(request, self.template_name, ctx)

    def post(self, request, pk):
        target = self.get_object()
        if target == request.user:
            messages.error(request, _("You cannot delete your own account."))
            return redirect("accounts:user_detail", pk=pk)

        with transaction.atomic():
            # 1. Topology auto-revoke (per Assignment einen Audit-Event)
            freed_positions = _revoke_all_topology(request, target)
            # 2. Account-Tokens invalidieren (Welcome/Reset/Verify)
            target.account_tokens.filter(used_at__isnull=True).update(
                used_at=timezone.now()
            )
            # 3. SSO-Grants + Sessions revoken (existing helper aus SSO-Spec)
            _revoke_sso(request, target)
            # 4. Group-Memberships entfernen
            target.groups.clear()
            # 5. Soft-Delete-Stempel + Login sperren
            target.deleted_at = timezone.now()
            target.deleted_by = request.user
            target.is_active = False
            target.save(update_fields=[
                "deleted_at", "deleted_by", "is_active",
            ])
            # 6. USER_SOFT_DELETED-Audit
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_SOFT_DELETED,
                actor=request.user,
                target_user=target,
                message=f"{target.username} <{target.email}>",
                ip_address=_client_ip(request),
            )

        if freed_positions:
            lines = "\n".join(f"  • {p}" for p in freed_positions)
            messages.warning(
                request,
                _("User soft-deleted. Free positions:\n%(lines)s\nReassign as needed.")
                % {"lines": lines},
            )
        else:
            messages.success(request, _("User soft-deleted."))
        return HttpResponseRedirect(reverse("accounts:user_list") + "?show=deleted")
```

**Hinweis:** Der Redirect zum List-View mit `?show=deleted` macht den Soft-Delete direkt sichtbar — Admin landet auf der Liste der gelöschten User, sieht sein gerade-deleted Mitglied oben.

### 4.2 Helper `_revoke_all_topology(request, user)`

```python
def _revoke_all_topology(request, user):
    """Auto-revoke alle Region- + Station-Assignments des Users.

    Emittiert pro Assignment den passenden *_ASSIGNMENT_REVOKED-Audit
    mit message="reason=user_soft_deleted" als Cluster-Marker.

    Returnt eine Liste menschenlesbarer Strings ("Station-Admin: OE5XRX")
    die im Success-Banner gezeigt werden, damit der Admin weiß welche
    Positionen jetzt nachzubesetzen sind.
    """
    freed = []
    for assignment in user.region_assignments.select_related("region"):
        freed.append(
            f"Region-{assignment.get_role_display()}: {assignment.region.name}"
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
            actor=request.user,
            target_user=user,
            message=(
                f"reason=user_soft_deleted region={assignment.region.name} "
                f"role={assignment.role}"
            ),
            ip_address=_client_ip(request),
        )
        assignment.delete()

    for assignment in user.station_assignments.select_related("station"):
        label = assignment.station.callsign or assignment.station.name
        freed.append(
            f"Station-{assignment.get_role_display()}: {label}"
        )
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
            actor=request.user,
            target_user=user,
            message=(
                f"reason=user_soft_deleted station={label} role={assignment.role}"
            ),
            ip_address=_client_ip(request),
        )
        assignment.delete()
    return freed
```

### 4.3 Helper `_revoke_sso(request, user)`

```python
def _revoke_sso(request, user):
    """Revoke alle SSO-Grants und terminate alle SSO-Sessions des Users."""
    now = timezone.now()
    if hasattr(user, "app_grants"):
        user.app_grants.filter(revoked_at__isnull=True).update(
            revoked_at=now, revoked_by=request.user,
        )
    if hasattr(user, "token_sessions"):
        user.token_sessions.filter(revoked_at__isnull=True).update(
            revoked_at=now, revoked_by=request.user,
        )
    # Per-grant + per-session Audit-Events folgen dem existing SsoAuditLog-
    # Pattern (siehe apps/sso/views.py); für Soft-Delete reicht
    # USER_SOFT_DELETED + die Counts im Impact-Banner — die SsoAuditLog-
    # Events wären redundant, weil der Cluster bereits über target_user
    # auflösbar ist.
```

**Hinweis:** Wir emittieren KEINE per-grant SSO-Audit-Events beim Soft-Delete (anders als bei den per-Assignment Topology-Audits), weil:
- Die SsoAuditLog hat ein eigenes Schema, das beim manuellen Grant-Revoke benutzt wird.
- Der USER_SOFT_DELETED-Audit + die `n_sso_grants`/`n_active_sessions`-Counts in der Impact-Anzeige reichen für die Forensik.

Wenn das mal ein Pain-Point wird, kann ein späterer Spec das nachziehen.

### 4.4 Confirm-Template `user_confirm_soft_delete.html`

Mirrort das heutige `user_confirm_delete.html` aus 1c, ändert nur:

- Page-Eyebrow: `"Soft-delete user"` statt `"Delete user"` (Verb tonality reflektiert Reversibilität).
- Page-Sub: `"This action is reversible — restore via the user list with ?show=deleted. Hard-purge is only available after a successful soft-delete."`
- Button-Text: `"Soft-delete"` (button-danger styled).
- Cancel führt zurück auf `user_detail`.
- Counts-Section + Station-Admin-Warning bleiben unverändert aus 1c — der Audit-Impact ist dieselbe Information.

---

## 5. Restore-View

### 5.1 `UserRestoreView` — POST-only

```python
class UserRestoreView(AdminRequiredMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        target = get_object_or_404(
            User, pk=pk, deleted_at__isnull=False,
        )
        with transaction.atomic():
            target.deleted_at = None
            target.deleted_by = None
            target.is_active = True
            target.save(update_fields=[
                "deleted_at", "deleted_by", "is_active",
            ])
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_RESTORED,
                actor=request.user,
                target_user=target,
                message=f"{target.username} <{target.email}>",
                ip_address=_client_ip(request),
            )
        messages.success(
            request,
            _("User %(name)s restored. Topology assignments were revoked at "
              "delete-time and need to be re-assigned.") % {"name": target.username},
        )
        return redirect("accounts:user_detail", pk=pk)
```

**Was NICHT restored wird:**

- **Topology-Zuweisungen** (Station-/Region-Assignments) — wurden beim Soft-Delete revoked; Admin vergibt sie neu via die existing 1c-Cards auf `user_form.html`. Der Success-Banner sagt das explizit.
- **SSO-Grants** — revoked geblieben; Admin re-approved via die existing AppGrant-UI.
- **Group-Memberships** — `groups.clear()` lief; Admin re-assigniert via die existing Group-Picker-Card.
- **Account-Tokens** — alle invalidiert; wenn der User vor dem Delete einen pending Welcome/Reset hatte, kann der Admin den Welcome-Resend-Button auf `user_detail` benutzen (aus 2a) — `has_usable_password()` ist preserved, also weiß der Mechanismus woran er ist.

**Was wiederkommt automatisch:**

- **Passwort-Hash** — bleibt auf dem User-Row erhalten. Wenn der restored User vor Delete ein Passwort hatte, kann er sofort wieder einloggen.
- **Profil-Daten** (bio/avatar/qth/address/locator/lat/lon) — alles unverändert. Soft-Delete ist nur ein Flag, kein Scrub.
- **Membership-Level** — bleibt erhalten. Admin kann ihn natürlich anpassen.
- **`username`/`email`** — bleiben. Wenn in der Zwischenzeit ein neuer User dieselbe Email angelegt hat (das ist erlaubt — `__iexact` excludiert deleted), entsteht beim Restore ein Email-Konflikt: zwei aktive User mit gleicher Email.

### 5.2 Email-Konflikt beim Restore

Edge-Case: User Alice wird soft-deleted (email=alice@example.org). Admin legt parallel einen anderen User Bob mit derselben Email an. Jetzt klickt Admin Restore auf Alice.

**Lösung:** Vor dem Restore-Commit checken:

```python
clashing = User.objects.active().filter(
    email__iexact=target.email
).exclude(pk=target.pk).first()
if clashing:
    messages.error(
        request,
        _("Cannot restore: another active user (%(other)s) is using %(email)s. "
          "Either change %(other)s's email first, or update %(name)s's email "
          "before restoring.") % {
            "other": clashing.username,
            "email": target.email,
            "name": target.username,
        },
    )
    return redirect("accounts:user_detail", pk=pk)
```

Username-Konflikt ist DB-erzwungen — die `unique_active_username`-Constraint feuert beim Save, was wir vorab via `.active().filter(username__iexact=...).exists()`-Check abfangen und in eine human-readable Error-Message übersetzen.

---

## 6. Hard-Purge-View

### 6.1 `UserHardPurgeView`

```python
class UserHardPurgeView(AdminRequiredMixin, View):
    template_name = "accounts/user_confirm_hard_purge.html"

    def get_object(self):
        # Critical guard: nur soft-deleted User sind hard-purgeable.
        # Active user → 404. Kein UI-Pfad zeigt diese URL für aktive User.
        return get_object_or_404(
            User, pk=self.kwargs["pk"], deleted_at__isnull=False,
        )

    def get(self, request, pk):
        target = self.get_object()
        return render(request, self.template_name, {
            "target_user": target,
            "deleted_at": target.deleted_at,
            "deleted_by": target.deleted_by,
            "n_audit_as_actor": AccountAuditLog.objects.filter(actor=target).count(),
            "n_audit_as_target": AccountAuditLog.objects.filter(target_user=target).count(),
        })

    def post(self, request, pk):
        target = self.get_object()
        # Audit BEFORE delete — der target FK ist nach .delete() weg
        username = target.username
        email = target.email
        deleted_at = target.deleted_at
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_HARD_PURGED,
            actor=request.user,
            target_user=target,
            message=(
                f"{username} <{email}> "
                f"(purged after soft-delete on {deleted_at:%Y-%m-%d})"
            ),
            ip_address=_client_ip(request),
        )
        # Avatar-File physisch entfernen
        if target.avatar:
            try:
                target.avatar.delete(save=False)
            except Exception:
                logger.exception(
                    "Avatar file delete failed for purged user %s",
                    target.pk,
                )
        # Cascade-Effekte:
        # - AccountToken: CASCADE → alle Token-Rows weg.
        # - StationAssignment/RegionAssignment: existieren nicht mehr
        #   (wurden beim Soft-Delete revoked).
        # - AccountAuditLog.actor/target: SET_NULL → bleiben mit NULL
        #   als Tombstone; die message-Strings preservieren username
        #   und email textuell für historische Lesbarkeit.
        # - AppGrant, OidcAppPolicy etc.: SET_NULL/CASCADE je nach
        #   Modell — Verhalten aus 1a/SSO-Spec.
        target.delete()
        messages.success(request, _("User permanently purged."))
        return HttpResponseRedirect(reverse("accounts:user_list") + "?show=deleted")
```

### 6.2 Confirm-Template `user_confirm_hard_purge.html`

Mirrort `user_confirm_soft_delete.html`, mit Anpassungen:

- Page-Eyebrow: `"Hard-purge — irreversible"` (red, danger).
- Display-Block: `"Soft-deleted on {{ deleted_at|date:'Y-m-d' }} by {{ deleted_by.username|default:'(unknown)' }}"`.
- Impact-Block: zeigt `n_audit_as_actor` + `n_audit_as_target` als "Audit-Rows die zu Tombstones werden" (mit Hinweis dass die Strings im message-Feld textuell überleben).
- Button-Text: `"Permanently purge"` (extra-loud red).
- Cancel führt zurück auf `user_detail`.

---

## 7. UserListView + UserDetailView — UI-Konsolidierung

### 7.1 List wird zur Browse-Surface

`UserListView` wird neu zusammengesetzt:

```python
class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def get_queryset(self):
        show = self.request.GET.get("show", "active")
        qs = User.objects.order_by("username")
        if show == "deleted":
            qs = qs.filter(deleted_at__isnull=False)
        elif show == "inactive":
            qs = qs.filter(deleted_at__isnull=True, is_active=False)
        elif show == "all":
            pass
        else:  # default "active"
            qs = qs.filter(deleted_at__isnull=True, is_active=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["filter_show"] = self.request.GET.get("show", "active")
        return ctx
```

**`user_list.html` Änderungen:**

- Filter-Bar oben (vier Pills: Active / Inactive / Deleted / All) — aktive Pill wird via `pill-accent` markiert.
- Pro Zeile: clickbarer Username-Link → `UserDetailView`. **Alle per-row Action-Buttons fliegen raus.**
- Status-Pill pro User: `ACTIVE` (online-grün) / `INACTIVE` (grau) / `DELETED YYYY-MM-DD` (gedämpft-rot).
- Das alte `_user_actions.html`-Partial (falls existiert) wird gelöscht.

### 7.2 Detail wird zur Action-Surface

`UserDetailView` aus 1b bleibt — nur:
- Der heutige 404-Filter (falls einer ist) wird entfernt; deleted User sind erreichbar.
- Template `user_detail.html` rendert Action-Bar conditional:

| User-State | Action-Bar Buttons |
|---|---|
| Active OR Inactive (`deleted_at IS NULL`) | **Edit** + **Soft-Delete** |
| Soft-deleted | **Restore** (POST-Form) + **Hard-Purge** (Link auf Confirm-Page) + Edit-Button **disabled mit Tooltip** `"Restore first"` |
| Self (logged-in User schaut sein eigenes Detail) | **Edit Profile** (führt auf `/profile/`) — Soft-Delete-Button NICHT gerendert (Self-Block) |

**Über der Action-Bar** ein Banner:
- Active: `"Active member since {{ user.date_joined|date:'Y-m-d' }}"` — neutral / green.
- Soft-deleted: `"Soft-deleted on {{ user.deleted_at|date:'Y-m-d H:i' }} by {{ user.deleted_by.username|default:'(unknown)' }}"` — danger-red, mit border-left-Style aus 1c's `.onboarding-hint`.

**Existing Cards bleiben:** Membership-Card, Region-Assignments, Station-Assignments, Sessions, Audit-Tab, "Resend Welcome" aus 2a. Bei deleted Usern sind alle Card-Aktionen (Promote, Assign, Revoke etc.) automatisch broken, weil die Cards `target_user.deleted_at IS NULL` voraussetzen — wir disablen sie konditional im Template (`{% if not target_user.deleted_at %}…{% endif %}`).

---

## 8. URLs + Audit-Events

### 8.1 URL-Map (`apps/accounts/urls.py`)

```python
# ENTFERNEN:
# path("users/<int:pk>/delete/", UserDeleteView.as_view(), name="user_delete"),

# NEU:
path(
    "users/<int:pk>/soft-delete/",
    UserSoftDeleteView.as_view(),
    name="user_soft_delete",
),
path(
    "users/<int:pk>/restore/",
    UserRestoreView.as_view(),
    name="user_restore",
),
path(
    "users/<int:pk>/hard-purge/",
    UserHardPurgeView.as_view(),
    name="user_hard_purge",
),
```

URL-Name `user_delete` → `user_soft_delete`. Templates die das URL referenzieren (`user_detail.html`, `user_form.html`, ggf. andere) werden im selben PR aktualisiert.

### 8.2 Neue `EventType`-Choices

```python
class EventType(models.TextChoices):
    # ... bestehende ...
    USER_SOFT_DELETED = "user_soft_deleted", _("User Soft-Deleted")
    USER_RESTORED     = "user_restored",     _("User Restored")
    USER_HARD_PURGED  = "user_hard_purged",  _("User Hard-Purged")
    # USER_DELETED bleibt in der Choices-Liste als deprecated marker —
    # wird nicht mehr emittiert, aber alte DB-Rows referenzieren den
    # String und brauchen ihn für display.
```

### 8.3 Audit-Message-Patterns

| Event | actor | target | message |
|---|---|---|---|
| `USER_SOFT_DELETED` | admin | user | `f"{user.username} <{user.email}>"` |
| `USER_RESTORED` | admin | user | `f"{user.username} <{user.email}>"` |
| `USER_HARD_PURGED` | admin | user (vor `.delete()`) | `f"{user.username} <{user.email}> (purged after soft-delete on YYYY-MM-DD)"` |
| `REGION_ASSIGNMENT_REVOKED` (auto-revoke beim Soft-Delete) | admin | user | `f"reason=user_soft_deleted region={name} role={role}"` |
| `STATION_ASSIGNMENT_REVOKED` (auto-revoke beim Soft-Delete) | admin | user | `f"reason=user_soft_deleted station={callsign_or_name} role={role}"` |

Das `reason=user_soft_deleted`-Marker im Message-Feld macht den Cluster im Audit-Feed maschinen- und auch menschen-suchbar — alle Audit-Rows, die zu einer Soft-Delete-Aktion gehören, lassen sich filtern.

---

## 9. Tests (~22 neue Tests, 4 Module)

### 9.1 `tests/test_user_soft_delete.py` (~9 Tests)

```
class TestSoftDeleteConfirmGET:
    test_get_shows_counts
    test_get_shows_station_admin_warning_list
    test_active_user_returns_200
    test_soft_deleted_user_returns_404

class TestSoftDeletePOST:
    test_post_sets_deleted_at_and_deleted_by_and_is_active_false
    test_self_soft_delete_blocked
    test_topology_auto_revoked_with_per_assignment_audit
    test_account_tokens_invalidated
    test_emits_user_soft_deleted_audit_with_email_in_message
```

### 9.2 `tests/test_user_restore.py` (~4 Tests)

```
class TestRestore:
    test_restore_sets_deleted_at_null_and_is_active_true
    test_active_user_returns_404
    test_restore_blocked_when_email_conflicts_with_active_user
    test_emits_user_restored_audit
```

### 9.3 `tests/test_user_hard_purge.py` (~5 Tests)

```
class TestHardPurge:
    test_active_user_returns_404
    test_post_cascades_account_tokens
    test_post_sets_audit_actor_and_target_to_null_but_message_preserves_strings
    test_post_deletes_avatar_file
    test_emits_user_hard_purged_audit_with_soft_delete_date_in_message
```

### 9.4 `tests/test_user_list_filter.py` (~4 Tests)

```
class TestUserListFilter:
    test_default_shows_active_only
    test_show_inactive_shows_inactive_only
    test_show_deleted_shows_deleted_only
    test_show_all_shows_everyone
```

### 9.5 Bestehende Tests, die anpassen/entfernen müssen

| Test | Anpassung |
|---|---|
| `tests/test_user_delete_view.py` (aus 1c) | **Komplett löschen** — die alte `UserDeleteView` gibt's nicht mehr. Coverage geht zu `test_user_soft_delete.py` + `test_user_hard_purge.py`. |
| Templates die `{% url 'accounts:user_delete' ... %}` referenzieren | URL-Name aktualisieren zu `user_soft_delete`. |
| `tests/test_user_change_form.py::TestUserCreationFormClean` (aus 2a) | Neuer Test: `clean_email` / `clean_username` excludieren soft-deleted User aus Uniqueness-Check (neuer User mit gleicher Email/Username eines soft-deleted Users muss erlaubt sein). |
| `tests/test_account_token.py` (aus 2a) | Neuer Test: Account-Tokens werden beim Soft-Delete invalidiert. |

### 9.6 Coverage-Patterns

**Auto-Revoke-Cluster (`test_user_soft_delete::test_topology_auto_revoked_with_per_assignment_audit`):**

```python
# Setup: User mit 1 Station-Admin + 1 Region-Manager + 1 Station-Maintainer
# (insgesamt 3 Assignments).
# Act: POST /accounts/users/<pk>/soft-delete/
# Assert:
#   - target_user.station_assignments.exists() == False
#   - target_user.region_assignments.exists() == False
#   - 3 *_ASSIGNMENT_REVOKED-Audit-Rows mit message contains "reason=user_soft_deleted"
#   - messages.warning enthält "Free positions:" + 3 Zeilen
```

**Tombstone-Preservation (`test_user_hard_purge::test_post_sets_audit_actor_and_target_to_null_but_message_preserves_strings`):**

```python
# Setup: Admin loggt sich ein, soft-deleted Bob (1 USER_SOFT_DELETED-Audit
# mit actor=admin, target=bob, message="bob <bob@…>" entsteht).
# Soft-delete: admin loggt ein, soft-deleted bob.
# Hard-purge: admin loggt ein, hard-purged bob.
# Assert:
#   - User.objects.filter(pk=bob.pk).exists() == False
#   - USER_SOFT_DELETED-Audit-Row hat target=NULL, message bleibt
#     "bob <bob@example.org>" (textuell)
#   - USER_HARD_PURGED-Audit-Row hat target=NULL, message enthält
#     "bob <bob@example.org> (purged after soft-delete on …)"
```

---

## 10. Out-of-Scope

- **Anonymisierung / PII-Scrubbing** beim Soft-Delete. Profil-Daten (bio, avatar, address, phone, qth) bleiben unverändert. DSGVO-Recht-auf-Vergessen erfordert manuelles Hard-Purge durch den Admin. Wenn Anonymisierung als Self-Service gewünscht: separater Spec.
- **Auto-Purge nach Retention-Frist** — kein Cron-Job der nach 30/60/90 Tagen Soft-Deleted hard-purgt. Admin macht das manuell. Wenn das mal ein Pain-Point wird (z.B. Bewerber-Karteileichen sammeln sich): Follow-up-Spec mit konfigurierbarer Retention.
- **Self-Delete-Endpoint** — kein "Account löschen"-Button im Profil. Verein-Politik: Austritte gehen über den Vorstand, nicht Self-Service.
- **Email-Benachrichtigung an Vereins-Admins** bei freigewordenen Topology-Positionen — In-App-Banner aus dem Success-Pfad reicht. Optional als separater Spec.
- **Bulk-Operations** — kein "alle inaktiven User > 2 Jahre soft-deleten"-Tool, kein "alle soft-deleted > 90 Tage hard-purgen"-Tool. Pro User einzeln.
- **Restore-mit-Topology** — Topology-Assignments werden beim Restore NICHT auto-zurückgegeben. Admin vergibt sie neu. Spart "Wem gehörte was"-Buchhaltung.
- **`USER_DELETED`-Audit-Migration** — alte Pre-2b Audit-Rows behalten `event_type=user_deleted`. Sie werden nicht zu `user_hard_purged` umgeschrieben. Choice bleibt im Enum deprecated.
- **`session`/Session-Backend-Cleanup** — Django-Session-Rows aus `django_session` werden nicht aktiv invalidiert beim Soft-Delete; sie expirieren wie üblich. Login ist gesperrt via `is_active=False`, die existing Session ist also wertlos.

---

## 11. Risiken + Mitigation

| Risiko | Wahrscheinlichkeit | Impact | Mitigation |
|---|---|---|---|
| Soft-deleted User klickt alten Reset-Link aus seiner Inbox | mittel | Kein Login möglich (`is_active=False` blockt eh), aber `consume_token` würde theoretisch das Token konsumieren | Account-Tokens werden beim Soft-Delete invalidiert (§4.1 Schritt 2) — `consume_token` returnt None |
| Soft-deleted User hat aktive SSO-Session zu InvenTree | hoch | User bleibt in InvenTree weiter eingeloggt bis InvenTree-Session expired | `_revoke_sso` terminiert die SSO-Token-Sessions. InvenTree-side Session läuft je nach InvenTree-Config aus (max 8h Standard). Akzeptabel — Spec-Boundary endet bei unserem IDp |
| Race: Admin A klickt Restore während Admin B klickt Hard-Purge auf denselben deleted User | sehr niedrig | Letzte Tx wins; der andere sieht Error | Beide Views nutzen `transaction.atomic()`. Wenn Hard-Purge zuerst commit'et, sieht Restore einen 404 beim 2. Lookup. Wenn Restore zuerst commit'et, sieht Hard-Purge einen 404 (`deleted_at` ist jetzt NULL). Beide enden in einer human-readable Error-Message |
| Email/Username-Konflikt beim Restore (siehe §5.2) | niedrig | Restore failt, Admin muss erst den parallelen User anpassen | Vor Restore-Commit Check; Error-Message zeigt den parallelen User + Anweisung |
| Avatar-File-Delete failt bei Hard-Purge (z.B. S3 transient) | niedrig | Stale-File bleibt orphan im Storage | `target.avatar.delete()` ist in try/except; DB-Delete commit'et trotzdem. Future: storage-Audit/-Cleanup-Job |
| Conditional UNIQUE-Index auf username nicht vom Migration-Path supported in SQLite | hoch in dev (SQLite), gar nicht in prod (Postgres) | Test-Suite mit SQLite-Backend bricht | Django emittiert für SQLite ein partielles Index-Statement das ab SQLite 3.8 supported ist — modern enough. Prüfen mit `manage.py sqlmigrate` |
| `_revoke_all_topology` ist nicht idempotent — bei erneutem Lauf wären die Assignments schon weg | niedrig | Re-Soft-Delete (sollte 404 sein) würde aber doch nicht ausgeführt | `get_object()` filtert auf `deleted_at__isnull=True`; Re-Soft-Delete kommt nie an `_revoke_all_topology` heran |

---

## 12. Migrations + Deploy

**Eine Django-Migration:** `0XXX_user_soft_delete.py` — 2 Operations (AddField × 2 + Constraint-Swap). Keine Datenmigrationen. Reversibel (rollback dropt die Felder + restored den unconditional unique-Index).

**Deploy-Sequenz:** Standard via `gh workflow run main.yml` im `servers`-Repo nach Merge. Migration läuft im `web`-Container `entrypoint` (`manage.py migrate`). Da kein Daten-Touch passiert und neue Felder nullable sind, ist das ein No-Downtime-Deploy.

**Bitwarden-Secrets:** Keine neuen Secrets.

**Server-Yaml** (`servers/services/station_manager/service.yaml`): Keine Änderung.

---

## 13. Implementierungs-Reihenfolge (für Plan-Phase)

Vorschlag für die spätere Plan-Phase:

1. **Datenmodell + Migration + Manager-Helper** — Felder addieren, Constraint-Swap, `User.objects.active()`/`deleted()` einbauen. Tests: `test_user_list_filter.py` (für die Manager-Helper) sind separat, aber Migration kann mit existing Tests verifiziert werden.
2. **Forms-Anpassung** — `UserCreationForm.clean_email/clean_username` excludiert soft-deleted; `ProfileIdentityForm.clean_email` analog. Tests-Anpassung in `test_user_change_form.py`.
3. **Soft-Delete-View + Helper** — `UserSoftDeleteView`, `_revoke_all_topology`, `_revoke_sso`, neue Audit-Events. Tests: `test_user_soft_delete.py`.
4. **Restore-View** — `UserRestoreView` mit Email/Username-Konflikt-Check. Tests: `test_user_restore.py`.
5. **Hard-Purge-View** — `UserHardPurgeView` mit `deleted_at__isnull=False`-Guard + Avatar-File-Delete. Tests: `test_user_hard_purge.py`.
6. **UserListView Filter + Template** — `?show=`-Param, Filter-Bar, Action-Buttons aus dem List-Template entfernen. Tests: `test_user_list_filter.py`.
7. **UserDetailView Action-Surface + Template** — Banner + konditionale Action-Bar, Card-Disabling für deleted User.
8. **Topology-Notification-Filter** — `apps/monitoring/recipients.py` + `apps/accounts/visibility.py` ergänzen um `deleted_at__isnull=True`-Filter.
9. **Cleanup** — `tests/test_user_delete_view.py` löschen, alte Templates aktualisieren, ruff format, CHANGELOG.

Step 1, 2 sind sequenziell. Step 3, 4, 5 können parallel laufen (Subagents), wenn die Migration aus Step 1 schon drin ist. Step 6, 7 hängen von 3-5 ab. Step 8 ist orthogonal — kann jederzeit nach Step 1 laufen.

---

**Spec Owner:** Peter Buchegger
**Letzte Änderung:** 2026-06-13
