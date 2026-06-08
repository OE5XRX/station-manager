"""Custom OIDC claims emitted in ID tokens and UserInfo responses.

Two DOT hooks funnel through the same function: ID-token claims via
``SsoOAuth2Validator.get_additional_claims`` and UserInfo claims via
``OAUTH2_PROVIDER["OIDC_USERINFO_HOOK"]``. Keeping both paths through
this one function means RPs see identical data regardless of which
endpoint they use.

The ``groups`` claim is synthesised from four sources (Spec §5):

- ``membership_level`` -> ``"applicant"``/``"member"``/``"staff"``/``"admin"``
- ``StationAssignment`` -> ``"station:<pk>:<role>"``  (Station has no slug
  field; the numeric primary key is used as the stable identifier.)
- ``RegionAssignment``  -> ``"region:<slug>:<role>"``
- Django ``auth.Group``  -> ``"tag:<name>"``
"""


def _build_groups(user) -> list[str]:
    """Synthetische groups-Liste -- siehe Spec §5.2.

    Determinismus: die Liste wird sortiert+dedupliziert zurueckgegeben,
    damit Test-Stabilitaet und RP-Diff-Sauberkeit gegeben sind.

    Quellen (in dieser Reihenfolge im Code, am Ende sortiert):
      1. ``user.membership_level`` -- alle vier Werte werden propagiert,
         auch ``"applicant"`` (Spec §5.1 Use-Case Einsteiger-Trainings).
      2. StationAssignments -- ``station:<pk>:<role>``. Station hat
         (Stand 2026-06) kein ``slug``-Feld; die PK ist der stabile
         Identifier. ``select_related("station")`` haelt die Query
         flach (kein N+1).
      3. RegionAssignments -- ``region:<slug>:<role>``. Region hat
         einen ``slug``, der admin-pflegbar und semantisch ist.
      4. Django ``auth.Group`` -- ``tag:<name>``. Praefix verhindert
         Kollisionen mit membership/station/region Strings, falls ein
         Admin versehentlich eine Group ``member`` anlegt.
    """
    groups: list[str] = []

    # 1. Membership-Level: alle vier Werte werden propagiert. Spec §5.1
    #    Use-Case (Applicant-Einsteiger-Trainings).
    groups.append(user.membership_level)

    # 2. StationAssignments -- Station hat kein slug, also pk verwenden.
    for assignment in user.station_assignments.select_related("station"):
        groups.append(f"station:{assignment.station.pk}:{assignment.role}")

    # 3. RegionAssignments
    for assignment in user.region_assignments.select_related("region"):
        groups.append(f"region:{assignment.region.slug}:{assignment.role}")

    # 4. Freie Django auth.Group-Tags
    for name in user.groups.values_list("name", flat=True):
        groups.append(f"tag:{name}")

    return sorted(set(groups))


def add_claims(claims, user, request):
    """Merge OE5XRX-specific claims into the OIDC payload.

    ``claims`` is a dict the caller hands in; mutate-or-return is fine
    (we do both to be safe across DOT versions: return value is what
    DOT actually uses).
    """
    claims["preferred_username"] = user.username
    claims["email"] = user.email or ""
    claims["email_verified"] = bool(user.email)
    claims["name"] = user.get_full_name() or user.username
    claims["locale"] = getattr(user, "language", "en") or "en"
    claims["groups"] = _build_groups(user)
    return claims
