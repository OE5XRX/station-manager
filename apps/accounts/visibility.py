"""Audience-aware visibility for the User-Domain Member-Directory.

Central single-source-of-truth for who-sees-what. Templates, list-views,
and detail-views consume `audience_for()` and `directory_visible_fields()`
to render audience-appropriate output.

Specification: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""

import enum

from django.contrib.auth import get_user_model

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
    if viewer.is_admin:
        return Audience.ADMIN
    if viewer.pk == target.pk:
        # Self-Sicht. Applicant-Variante für die wenigen Stellen, wo
        # man unterscheiden muss (z.B. List-Filter, der Applicants
        # ausnimmt).
        if viewer.membership_level == User.MembershipLevel.APPLICANT:
            return Audience.APPLICANT
        return Audience.SELF
    # Cross-User-Sicht.
    if viewer.membership_level == User.MembershipLevel.APPLICANT:
        # Applicants sehen niemand außer sich selbst.
        return None
    if target.membership_level == User.MembershipLevel.APPLICANT:
        # Member sehen Applicants nicht (Bewerber bleiben „außerhalb").
        return None
    return Audience.MEMBER
