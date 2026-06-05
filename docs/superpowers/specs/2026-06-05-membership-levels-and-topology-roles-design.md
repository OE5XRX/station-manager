# Membership-Levels + Topology-Roles — Design

**Status:** Spec — pending implementation plan
**Branch:** `feat/membership-levels-and-topology-roles`
**Author:** Brainstormed 2026-06-05 with Peter Buchegger

## 1. Problem

The current authorization model has two limitations that block real-world OE5XRX operations:

1. **Roles are vereinsweit (Austria-wide), not regional.** A Vereins-Admin in Oberösterreich has the same rights on a Tirol station as on a station in OÖ — but they cannot physically respond to a Tirol-station alert in minutes. There is no concept of "Lisa is the Tirol-Manager" or "Franz is the responsible person for OE5XTR".
2. **No membership progression.** A new applicant either gets full member rights immediately (today: admin adds them to the `member` group) or none at all. The natural training arc — applicant → trained member → may operate stations — has no representation.

Both gaps cause the same surface symptom: **Alert-Email notifications are too coarse.** Today every alert goes to every `admin`-group user. A Tirol-station-offline alert wakes up the Salzburg admin who cannot act on it; the Tirol-on-site responsible person has no special notification because the system doesn't know who that is.

Secondary problem: the term **"operator"** in the current `operator` Django-group collides with the amateur-radio meaning of "operator" (= the person at the radio / "Funker"). New role names must avoid this term.

## 2. Current State (today, 2026-06-05)

- Custom `User` extends `AbstractUser` with `language` field.
- Three Django-Groups seeded by `apps/accounts/migrations/0002_role_to_groups.py`: `admin`, `operator`, `member`.
- Cached properties on `User`: `is_admin`, `is_operator`, `is_staff_member` (= admin OR operator), `group_names`.
- Permission-gates across ~13 call-sites use these properties or `groups__name=...` directly.
- `apps/monitoring/notifications.py:_send_email_notification` hardcodes `User.objects.filter(groups__name="admin")` as the recipient list.
- No `Region` concept anywhere. `Station` model has tags/photos/inventory but no geographic-or-organizational grouping.
- No self-service registration path (`UserCreationForm` is Django-Admin-only).
- Existing audit infrastructure: `StationAuditLog` (per-station events), `SsoAuditLog` (SSO/OIDC events), `apps/audit/views.py` merges both into a single feed with filters + CSV export.

## 3. Design — Two Orthogonal Axes

### 3.1 Axis 1: Membership-Level (User-global, sequential)

A single enum-valued field on `User`, with semantic progression:

| Level | Display | Meaning |
|---|---|---|
| `applicant` | Vereins-Bewerber | Newly registered, view-only. Cannot be assigned topology-roles. Cannot operate stations. |
| `member`    | Vereins-Mitglied | Trained member. Can be assigned topology-roles. Can operate stations (future `can_use_station` enforcement). |
| `staff`     | Vereins-Staff    | Vereinsweite operative role. Operative access to **every** station regardless of topology assignment. Does NOT receive alert emails by default. |
| `admin`     | Vereins-Admin    | Full system access including user management, region CRUD, SSO config, alert thresholds. |

The level is set by a Vereins-Admin via the user-detail page (promote/demote buttons). Self-service registration is out-of-scope for this PR; if added later, new users default to `applicant`.

### 3.2 Axis 2: Topology (where you are responsible)

Three new models, all orthogonal to membership-level:

#### Region (free-form, admin-managed)
- `name` (e.g. "Tirol", "Innviertel"), `slug`, `description`.
- CRUD via dedicated admin-only `/regions/` page.
- Stations reference Region via FK (nullable, `on_delete=SET_NULL`).

#### StationAssignment (per-station, per-user, per-role)
Two roles per station:
- `admin` — "Station-Admin": **max. 1 user per station** (hard DB constraint). The accountable owner.
- `maintainer` — "Station-Maintainer": **N users per station**. Co-helpers.
- Constraint: a single user has **max. 1 role per station** (cannot be both Station-Admin and Station-Maintainer of the same station).

#### RegionAssignment (per-region, per-user, per-role)
One role:
- `manager` — "Region-Manager": **N users per region**. Operative authority over all stations of the region.

### 3.3 Permission-Composition

A high-level "can-do-X-on-station-Y" check composes the two axes:

```python
def can_administer_station(user, station):
    return (
        user.is_internal                            # staff or admin, vereinsweit
        or user.is_station_admin(station)           # local owner
        or user.is_region_manager(station.region)   # regional authority
    )

def can_maintain_station(user, station):
    return (
        can_administer_station(user, station)
        or user.is_station_maintainer(station)
    )

def can_use_station(user, station):
    """Future hook for the radio-operation feature (Funken).
    Permission contract is defined now, no consumer yet."""
    return user.membership_level != MembershipLevel.APPLICANT
```

The `station` parameter is part of the `can_use_station` signature even though today's logic ignores it — so per-station/per-region restrictions can be added later without a breaking change.

### 3.4 Naming Conventions

**Database / code identifiers** use the short form (matches existing patterns in the codebase):
- `MembershipLevel.APPLICANT / MEMBER / STAFF / ADMIN`
- `StationAssignmentRole.ADMIN / MAINTAINER`
- `RegionAssignmentRole.MANAGER`

**UI display labels** use the axis-role compound form (resolves the `admin` ambiguity between Vereins and Station):
- `_("Vereins-Bewerber")`, `_("Vereins-Mitglied")`, `_("Vereins-Staff")`, `_("Vereins-Admin")`
- `_("Station-Admin")`, `_("Station-Maintainer")`
- `_("Region-Manager")`

## 4. Data Model

### 4.1 User (extension)

```python
class User(AbstractUser):
    class MembershipLevel(models.TextChoices):
        APPLICANT = "applicant", _("Vereins-Bewerber")
        MEMBER    = "member",    _("Vereins-Mitglied")
        STAFF     = "staff",     _("Vereins-Staff")
        ADMIN     = "admin",     _("Vereins-Admin")

    membership_level = models.CharField(
        max_length=10,
        choices=MembershipLevel.choices,
        default=MembershipLevel.APPLICANT,
    )
    # existing: language, ...
```

Properties on User (cached):
- `is_admin` — kept, semantics shift to `membership_level == ADMIN`. Backwards-compat for the dozen existing call-sites.
- `is_internal` — new. True if `membership_level in (STAFF, ADMIN)`. Replaces `is_staff_member` (renamed for clarity vs Django's built-in `is_staff`).
- `is_operator` — **removed**. Term collides with amateur-radio meaning. All call-sites refactored.
- `is_staff_member` — **removed**, replaced by `is_internal`.
- `group_names` — removed (no longer meaningful without the role-groups).

Per-station / per-region helpers (not cached, accept argument):
- `is_station_admin(station)` — True if active StationAssignment with role=ADMIN.
- `is_station_maintainer(station)` — analog.
- `is_region_manager(region)` — analog (returns False for `region=None`).
- `can_administer_station(station)`, `can_maintain_station(station)`, `can_use_station(station)` — composed predicates per Section 3.3.

### 4.2 Region

```python
# apps/stations/models.py
class Region(models.Model):
    name        = models.CharField(_("name"), max_length=80, unique=True)
    slug        = models.SlugField(_("slug"), unique=True)
    description = models.TextField(_("description"), blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name
```

`Station.region` added as nullable FK with `on_delete=SET_NULL`.

### 4.3 StationAssignment

```python
# apps/stations/models.py
class StationAssignment(models.Model):
    class Role(models.TextChoices):
        ADMIN      = "admin",      _("Station-Admin")
        MAINTAINER = "maintainer", _("Station-Maintainer")

    user        = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name="station_assignments")
    station     = models.ForeignKey(Station, on_delete=models.CASCADE,
                                    related_name="assignments")
    role        = models.CharField(max_length=12, choices=Role.choices)
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(User, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="assigned_station_assignments")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "station"],
                name="uniq_user_per_station_assignment",
            ),
            models.UniqueConstraint(
                fields=["station"],
                condition=Q(role="admin"),
                name="uniq_admin_per_station",
            ),
        ]
        indexes = [
            models.Index(fields=["station", "role"]),
            models.Index(fields=["user"]),
        ]

    def clean(self):
        super().clean()
        if self.user.membership_level == User.MembershipLevel.APPLICANT:
            raise ValidationError({"user": _(
                "Vereins-Bewerber können keine Topology-Rolle haben. "
                "Den User erst zu Vereins-Mitglied promoten."
            )})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
```

### 4.4 RegionAssignment

```python
# apps/stations/models.py
class RegionAssignment(models.Model):
    class Role(models.TextChoices):
        MANAGER = "manager", _("Region-Manager")

    user        = models.ForeignKey(User, on_delete=models.CASCADE,
                                    related_name="region_assignments")
    region      = models.ForeignKey(Region, on_delete=models.CASCADE,
                                    related_name="assignments")
    role        = models.CharField(max_length=10, choices=Role.choices)
    assigned_at = models.DateTimeField(auto_now_add=True)
    assigned_by = models.ForeignKey(User, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="assigned_region_assignments")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "region", "role"],
                name="uniq_user_role_per_region",
            ),
        ]
        indexes = [
            models.Index(fields=["region", "role"]),
            models.Index(fields=["user"]),
        ]

    def clean(self):
        super().clean()
        if self.user.membership_level == User.MembershipLevel.APPLICANT:
            raise ValidationError({"user": _(
                "Vereins-Bewerber können keine Topology-Rolle haben. "
                "Den User erst zu Vereins-Mitglied promoten."
            )})

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)
```

### 4.5 Applicant Invariant

Enforced on **two layers**, defense-in-depth:

1. Model-level `clean()` + `save()` override (as shown) — rejects creation when user is an applicant. Catches direct `.save()` paths that bypass forms.
2. Demote-flow validation — Vereins-Admin can only demote a user to `applicant` if the user has **zero** active StationAssignments and RegionAssignments. UI shows the blocking assignments; operator must remove them first. No implicit cascade-delete.

Recipient queries (Section 4.7) additionally `.exclude(membership_level=APPLICANT)` as a third safeguard, even though it should never match given the above invariants.

### 4.6 AccountAuditLog

New model, parallel to `StationAuditLog` and `SsoAuditLog`. Lives in `apps/accounts/models.py`.

```python
class AccountAuditLog(models.Model):
    """System-wide audit trail for account-management and topology events.

    Parallel to StationAuditLog (per-station) and SsoAuditLog (SSO/OIDC).
    apps/audit/ merges all three into a single feed.
    """
    class EventType(models.TextChoices):
        MEMBERSHIP_PROMOTED       = "membership_promoted",       _("Membership Promoted")
        MEMBERSHIP_DEMOTED        = "membership_demoted",        _("Membership Demoted")
        REGION_ASSIGNMENT_CREATED = "region_assignment_created", _("Region Assignment Created")
        REGION_ASSIGNMENT_REVOKED = "region_assignment_revoked", _("Region Assignment Revoked")
        REGION_CREATED            = "region_created",            _("Region Created")
        REGION_UPDATED            = "region_updated",            _("Region Updated")
        REGION_DELETED            = "region_deleted",            _("Region Deleted")

    event_type  = models.CharField(max_length=32, choices=EventType.choices)
    actor       = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="account_audit_logs_as_actor")
    target_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="account_audit_logs_as_target")
    region      = models.ForeignKey("stations.Region", null=True, blank=True,
                                    on_delete=models.SET_NULL,
                                    related_name="audit_logs")
    message     = models.TextField(blank=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["event_type", "-created_at"]),
            models.Index(fields=["target_user", "-created_at"]),
        ]

    @classmethod
    def log(cls, *, event_type, actor=None, target_user=None,
            region=None, message="", ip_address=None):
        return cls.objects.create(
            event_type=event_type, actor=actor, target_user=target_user,
            region=region, message=message, ip_address=ip_address,
        )
```

StationAuditLog gains three new event types (additive, no schema change):
- `STATION_ASSIGNMENT_CREATED`
- `STATION_ASSIGNMENT_REVOKED`
- `STATION_REGION_CHANGED`

`apps/audit/views.py` is updated to merge all three sources (currently merges two). The existing `MERGE_FEED_CAP` semantics extend naturally.

### 4.7 Notification Routing

New helper file `apps/monitoring/recipients.py` (single-purpose, unit-testable in isolation):

```python
def recipients_for_station_alert(station):
    """Resolve email recipients for an alert on `station`.

    Returns a deduplicated queryset of active Users with non-empty
    emails, comprising:
      - Vereins-Admins (membership_level=ADMIN, vereinsweit)
      - Region-Manager of station.region (if station.region is set)
      - Station-Admin of this station
      - Station-Maintainer of this station

    Vereins-Staff is NOT routed by default — it's an operative role,
    not an escalation inbox. Staff who want alerts must also hold a
    topology assignment.

    Applicants are excluded even if (somehow) they hold an assignment.
    """
    q = Q(membership_level=User.MembershipLevel.ADMIN)
    if station.region_id is not None:
        q |= Q(
            region_assignments__region_id=station.region_id,
            region_assignments__role="manager",
        )
    q |= Q(
        station_assignments__station=station,
        station_assignments__role__in=["admin", "maintainer"],
    )
    return (
        User.objects
        .filter(q)
        .exclude(email="")
        .exclude(is_active=False)
        .exclude(membership_level=User.MembershipLevel.APPLICANT)
        .distinct()
    )
```

`apps/monitoring/notifications.py:_send_email_notification(alert)` is rewritten to call this helper. If the recipient set is empty, a `WARNING` is logged with station + region context so operators can diagnose missing assignments.

**Test-Email path change:** `send_test_notification("email")` is updated to send to the requesting user only (the admin who clicked the button), not to all admins. Removes cross-notification noise during diagnostics.

**Telegram routing is unchanged** in this PR — remains a single global `TELEGRAM_CHAT_ID` for the Vereinsgruppe channel.

## 5. UI Changes

### 5.1 User-Detail Page (`/users/<id>/`)

Existing page extended with three new sections:

- **Vereins-Rolle** — shows current membership level + a dropdown of all valid target levels. Vereins-Admin can leap arbitrarily (e.g. `applicant → admin` in one step) — the audit log captures the from-to delta either way. Never visible on self-view (no self-promotion).
- **Region-Manager für** — list of regions where the user is a manager, with remove (✕) buttons; add-button opens a dropdown of all regions.
- **Station-Zuordnungen** — list of (Station, Role) tuples with remove buttons; add-button opens a 2-step dropdown (station → role). If role=ADMIN and the station already has an admin, a confirm-modal appears ("Take over from Franz?").

All write actions go through HTMX inline POSTs, write an AccountAuditLog (membership) or StationAuditLog (station-assignment) entry, and refresh the relevant section.

### 5.2 Station-Detail Page (`/stations/<id>/`)

Existing page gains a "Region & Verantwortliche" section:

- **Region** dropdown (admin-only) — change writes `STATION_REGION_CHANGED` audit entry.
- **Station-Admin** field (single, max-1) — set/change/remove. Cardinality enforced both in UI and via DB constraint.
- **Station-Maintainers** list — add/remove rows.

Editable by:
- Vereins-Admin (always)
- Region-Manager of the station's region (within their region)
- Station-Admin of the station (can edit Maintainers, but not remove themselves — that's a Region-Manager / Vereins-Admin action)

### 5.3 Region-CRUD Page (`/regions/`) — NEW

Admin-only listing with inline create / edit / delete. Each row shows name, slug, station count. Delete with stations attached prompts a confirm-modal explaining the SET_NULL effect on those stations.

### 5.4 Permission Matrix

| Action | Vereins-Admin | Region-Manager | Station-Admin | Station-Maintainer | Member | Applicant |
|---|---|---|---|---|---|---|
| View user list | ✓ | ✓ | – | – | – | – |
| Promote/demote other user | ✓ | – | – | – | – | – |
| Modify other's assignments | ✓ | (in own region) | (own station: maintainer only) | – | – | – |
| View station detail | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Change station's region | ✓ | – | – | – | – | – |
| Set station-admin | ✓ | (in own region) | – | – | – | – |
| Set station-maintainer | ✓ | (in own region) | (own station) | – | – | – |
| Region CRUD | ✓ | – | – | – | – | – |

## 6. Migration / Cutover Strategy

Six new migrations, ordered:

| # | Migration | App | Type | Reverse |
|---|---|---|---|---|
| 0004 | `add_membership_level` | accounts | schema (add CharField, default APPLICANT, NOT NULL) | drop field |
| 0005 | `seed_membership_levels` | accounts | data (Group → membership_level mapping) | noop |
| 0006 | `add_region_and_station_fk` | stations | schema (new Region model + Station.region FK nullable) | drop |
| 0007 | `add_assignments` | stations | schema (StationAssignment + RegionAssignment with constraints) | drop |
| 0008 | `add_account_audit_log` | accounts | schema (new model) | drop |
| 0009 | `drop_legacy_role_groups` | accounts | data (delete `admin`/`operator`/`member` groups) | data (recreate via `get_or_create`) |

New `StationAuditLog` event-types (`STATION_ASSIGNMENT_CREATED`, `_REVOKED`, `STATION_REGION_CHANGED`) are added to the `TextChoices` enum in code only — they don't require a schema migration since `choices=` is not DB-enforced. The call-site refactor across ~13 files is also code-only.

**0005 mapping rules:**
- User in `admin` group → `membership_level = ADMIN`
- User in `operator` group → `membership_level = STAFF`
- User in `member` group → `membership_level = MEMBER`
- User in multiple of the above → highest precedence wins (admin > staff > member)
- User in none of the above → `membership_level = APPLICANT` (defensive — should not occur today; possible for SSO-bootstrapped users)

**0009 rollback safety:** Reverse-code re-creates the three groups via idempotent `get_or_create`, returning the schema to a recoverable state within the 30-day backup window. Empty groups have no effect after the call-site refactor.

**Call-site refactor (no migration, code-only, atomic in same PR):**

| Location | Before | After |
|---|---|---|
| `apps/accounts/models.py` | `is_admin`, `is_operator`, `is_staff_member`, `group_names` | `is_admin` (semantics adjusted), `is_internal`, plus topology helpers |
| `apps/stations/views.py:45` | `user.is_staff_member` | `can_maintain_station(station)` |
| `apps/stations/views.py:106` | `user.is_admin` | `can_administer_station(station)` |
| `apps/firmware/views.py:27` | `is_admin or is_operator` | `user.is_internal` |
| `apps/monitoring/views.py:19,26` | `is_admin`, `is_staff_member` | unchanged for class-level admin; per-station class-views switch to topology helpers where applicable |
| `apps/monitoring/notifications.py:16,108` | `User.objects.filter(groups__name="admin")` | `recipients_for_station_alert(alert.station)` (16); for test-email: `request.user` only (108) |
| `apps/audit/views.py:161` | `User.objects.filter(groups__name="admin")` | `User.objects.filter(membership_level=ADMIN)` |
| `apps/deployments/consumers.py:31` | `user.is_staff_member` | `user.is_internal` |
| `apps/tunnel/views.py:24` | `user.is_staff_member` | `user.is_internal` |
| `apps/tunnel/consumers.py:35` | `user.is_staff_member` | `user.is_internal` |
| `apps/sso/views.py:50` | `is_admin` | unchanged (semantics match) |
| `apps/api/views.py:130` | `user.is_staff_member` | `user.is_internal` |
| `apps/accounts/views.py:19` | `user.is_admin` | unchanged |

`tests/conftest.py` fixtures are refactored to set `membership_level` directly on the user objects instead of group assignment.

## 7. Testing Strategy

Coverage by area:

### Models & invariants
- `test_applicant_cannot_be_station_admin / maintainer / region_manager` — model-level rejection
- `test_demote_to_applicant_with_assignments_blocked` — demote validation
- `test_demote_to_applicant_clean_user_ok`
- `test_promote_applicant_to_member_then_assign` — happy path
- `test_uniq_admin_per_station_enforced` — DB-level constraint
- `test_user_can_have_only_one_role_per_station`

### Permission helpers
- `test_is_internal_for_each_level`
- `test_can_administer_station_*` — admin path, station-admin path, region-manager path, member-without-assignment path (False)
- `test_can_maintain_station_includes_maintainer`
- `test_can_use_station_excludes_applicant_only`

### Notification routing (`tests/test_alert_recipients.py`)
- `test_admin_always_recipient`
- `test_region_manager_only_own_region`
- `test_region_manager_in_set`
- `test_station_admin_in_set`
- `test_station_admin_other_station_not_in_set`
- `test_station_maintainer_in_set`
- `test_staff_not_recipient`
- `test_member_without_assignments_not_recipient`
- `test_applicant_never_recipient` — even with fixture-injected assignment, the recipient query excludes
- `test_dedup_same_user_multiple_roles`
- `test_inactive_user_excluded`
- `test_user_without_email_excluded`
- `test_no_region_only_admin_and_station_assignments`

### Audit logging
- `test_promote_emits_account_audit_log`
- `test_demote_emits_account_audit_log`
- `test_station_assignment_create_emits_station_audit_log`
- `test_station_assignment_revoke_emits_station_audit_log`
- `test_region_assignment_create_emits_account_audit_log`
- `test_region_crud_emits_account_audit_log`
- `test_audit_events_visible_in_merged_feed` — sanity over the updated 3-source merge

### Migrations (using django-test-migrations, already in the repo)
- `test_0005_seeds_membership_levels_from_groups` — admin/operator/member → ADMIN/STAFF/MEMBER
- `test_0005_user_with_no_group_becomes_applicant`
- `test_0005_user_in_multiple_groups_takes_highest`
- `test_0009_groups_deleted_after_migrate`

### UI (smoke-level, focused on permission enforcement)
- `test_promote_button_visible_only_to_vereins_admin`
- `test_region_crud_admin_only`
- `test_station_admin_can_edit_only_own_station_maintainers`
- `test_region_manager_can_edit_only_own_region_stations`

## 8. Out-of-Scope (this PR)

Deferred but explicitly recorded so the design is honest about its boundaries:

- **Funk-Stack on the station** (consumer of `can_use_station`): we define the permission; no caller yet.
- **Per-user Telegram routing**: Telegram remains a single global channel. Would need `User.telegram_chat_id` + onboarding.
- **Notification-preferences per user** (e.g., "only critical, no warnings", quiet hours): no preferences model.
- **Per-severity routing** (different recipients for warning vs critical): not built.
- **Self-service registration** (`/signup/`): no public signup. Default `APPLICANT` is in place for when it's added.

Never planned (out-of-scope-permanently for this design):

- **2-of-N approval for promotion**: one admin can promote/demote.
- **Soft-delete for assignments** (à la AppGrant `revoked_at`): we hard-delete. Audit log preserves history.
- **AppGrant migration to group-based**: AppGrant stays per-user. Orthogonal to this design.
- **`Station.region` as required**: stays nullable. Routing degrades gracefully (only Vereins-Admins + direct station assignments) and the UI shows a "region not set" warning.

## 9. Known Limitations After Merge

- The three legacy Django-Groups (`admin`/`operator`/`member`) are deleted in migration 0009. Within the 30-day backup window, rollback is possible (reverse-code re-creates the groups); afterwards not.
- Migration 0005 (Group → membership_level seed) runs without an HTTP context, so it does NOT emit `AccountAuditLog` entries for the initial mapping. The cutover itself is documented by the migration record in `django_migrations`; the audit trail for membership changes begins with the first post-deploy promote/demote.
- Region-Manager cannot remove other Region-Managers in the same region — only Vereins-Admin can. Explicitly hierarchical.
- Empty-station-region edge case: stations without a region route only to Vereins-Admins + direct station assignments. UI warns; operator action required to assign region.
- Existing `apps/accounts/migrations/0002_role_to_groups.py` is kept for history; not removed by this PR.

## 10. References

- Brainstorming session: this conversation, 2026-06-05.
- Existing audit infra: `apps/stations/models.py:270` (StationAuditLog), `apps/sso/models.py:65` (SsoAuditLog), `apps/audit/views.py` (merged feed).
- Existing role-checks: documented call-site table in Section 6.
- Related but orthogonal: `apps/sso/models.py:AppGrant` (per-user OIDC application gate) — not modified by this design.
