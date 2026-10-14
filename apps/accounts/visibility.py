"""Audience-aware visibility for the User-Domain Member-Directory.

Central single-source-of-truth for who-sees-what. Templates, list-views,
and detail-views consume `audience_for()` and `directory_visible_fields()`
to render audience-appropriate output.

Specification: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

import enum

from django.contrib.auth import get_user_model

from apps.accounts.permissions import Level, can, level_of

User = get_user_model()


class Audience(enum.Enum):
    """Four-tier audience model for the user-directory.

    ADMIN     — sees everything for any user.
    SELF      — sees own data; Applicant variant below.
    MEMBER    — sees other members' public fields (subject to is_directory_visible).
    APPLICANT — sees own data only; no list, no other-user views.
    """

    ADMIN = "admin"
    SELF = "self"
    MEMBER = "member"
    APPLICANT = "applicant"


def audience_for(viewer, target):
    """Return the Audience tier `viewer` has on `target`.

    Returns None when the viewer has no access — caller raises Http404
    to avoid existence-leaking via 403.

    Note: viewer.is_admin precedes the viewer-equals-target check, so a
    Vereins-Admin sieht sich selbst auch als ADMIN (nicht als SELF). Das
    ist erwünscht — Admin braucht beim Self-View dieselben Werkzeuge
    wie bei anderen.
    """
    if not viewer.is_authenticated:
        return None
    if can(viewer, "users", "manage"):
        return Audience.ADMIN
    if viewer.pk == target.pk:
        # Self-Sicht. Applicant-Variante für die wenigen Stellen, wo
        # man unterscheiden muss (z.B. List-Filter, der Applicants
        # ausnimmt).
        if level_of(viewer) == Level.APPLICANT:
            return Audience.APPLICANT
        return Audience.SELF
    # Cross-User-Sicht.
    if level_of(viewer) == Level.APPLICANT:
        # Applicants sehen niemand außer sich selbst.
        return None
    if Level.from_slug(target.membership_level) == Level.APPLICANT:
        # Member sehen Applicants nicht (Bewerber bleiben „außerhalb").
        return None
    return Audience.MEMBER


# === Field-Visibility-Sets ===========================================
#
# Strings here are *concept keys* the templates check, e.g.
#   {% if "phone" in visible_fields and object.phone %}…
# They mostly mirror User-Modell-Feldnamen, plus zusammengesetzte Keys
# wie "region_assignments", "date_joined_year", "sso_sessions_self".

# Sichtbar für jeden eingeloggten Member (wenn target.is_directory_visible).
# Reihenfolge mirroring der Overview-Tab-Anzeige.
PUBLIC_PROFILE_FIELDS = frozenset(
    {
        "username",  # = Rufzeichen / Callsign
        "first_name",
        "last_name",
        "email",
        "membership_level",
        "avatar",
        "bio",
        "qth_name",
        "locator",
        "qrz_url",
        "date_joined_year",  # nur Jahr, nicht das Datum
        "region_assignments",  # Pill-Liste
        "station_assignments",  # Pill-Liste
    }
)

# Self + Admin sehen die. Member nicht.
PRIVATE_PROFILE_FIELDS = frozenset(
    {
        "address",
        "phone",
        "latitude",
        "longitude",  # numerisch, Admin-Debug-Block + Self
        "language",
        "last_login",  # Self sieht eigenen; Admin sieht alle
        "is_active",  # Self sieht eigenen Aktivitätsstatus; Admin sieht alle
        "is_directory_visible",
    }
)

# Nur Admin sieht die.
ADMIN_ONLY_FIELDS = frozenset(
    {
        "sso_grants",
        "sso_sessions",
        "tag_memberships",
        "global_audit_actions",  # Promote/Demote, Region-/Station-Assignment-Mgmt
    }
)

# Reduzierter Set, wenn target.is_directory_visible=False und viewer Member.
MINIMAL_DIRECTORY_FIELDS = frozenset(
    {
        "username",
        "membership_level",
        "avatar",
    }
)


def directory_visible_fields(viewer, target):
    """Return the frozenset of concept-keys `viewer` may see on `target`.

    Templates / serializers consume this:
        if "phone" in visible_fields and target.phone:
            render(target.phone)
    """
    aud = audience_for(viewer, target)
    if aud is None:
        return frozenset()
    if aud == Audience.ADMIN:
        return PUBLIC_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS | ADMIN_ONLY_FIELDS
    if aud in (Audience.SELF, Audience.APPLICANT):
        # Self/Applicant: eigene private + public Felder (read-only).
        # ADMIN_ONLY_FIELDS bleiben außen vor; sso_sessions_self ist die
        # Read-Only-Self-Variante des SSO-Sessions-Cards.
        return PUBLIC_PROFILE_FIELDS | PRIVATE_PROFILE_FIELDS | frozenset({"sso_sessions_self"})
    # Audience.MEMBER:
    if not target.is_directory_visible:
        return MINIMAL_DIRECTORY_FIELDS
    return PUBLIC_PROFILE_FIELDS


def user_can_view_directory(viewer):
    """Gate for the UserListView: ``users.view`` on the permission registry."""
    return can(viewer, "users", "view")


def directory_queryset(viewer):
    """Users the ``viewer`` may see in the member directory.

    Single source for the UI user list (``UserListView``) and the users API so
    both return the same set:

    - Managers (``users.manage``): every non-soft-deleted user — active and
      deactivated, Applicants included. (The UI list narrows this further with
      its lifecycle buckets; its default bucket shows the active ones.)
    - Everyone else: active (``is_active``), non-soft-deleted, non-Applicant
      users only.
    """
    qs = User.objects.active()
    if not can(viewer, "users", "manage"):
        qs = qs.filter(is_active=True).exclude(membership_level=User.MembershipLevel.APPLICANT)
    return qs
