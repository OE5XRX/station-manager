"""The one place that defines who may do what.

Model (spec 2026-10-14-unified-permissions-design.md): every capability is an
*area* × one of three fixed verbs (view ⊆ use ⊆ manage), each mapped to a
minimum membership level. UI views, templates, the REST API and WebSockets all
read from here; nothing else compares membership levels for authorization.

The single assignment-based exception is :func:`can_override_station`.
"""

from __future__ import annotations

import dataclasses
from enum import IntEnum

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

VERBS: tuple[str, ...] = ("view", "use", "manage")


class Level(IntEnum):
    APPLICANT = 1
    MEMBER = 2
    STAFF = 3
    ADMIN = 4

    @property
    def slug(self) -> str:
        return self.name.lower()

    @classmethod
    def from_slug(cls, slug) -> Level | None:
        return _BY_SLUG.get(slug)


_BY_SLUG = {lvl.slug: lvl for lvl in Level}


@dataclasses.dataclass(frozen=True)
class Area:
    label: str
    view: Level | None = None
    use: Level | None = None
    manage: Level | None = None
    verb_labels: dict[str, str] = dataclasses.field(default_factory=dict)


A, M, S, AD = Level.APPLICANT, Level.MEMBER, Level.STAFF, Level.ADMIN

AREAS: dict[str, Area] = {
    "dashboard": Area(_("Dashboard"), view=A),
    "profile": Area(_("Eigenes Profil"), use=A),
    "stations": Area(
        _("Stationen"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Stationen anlegen/bearbeiten, Logbuch, Fotos, Station-Audit")},
    ),
    "control": Area(
        _("Stationssteuerung"),
        view=A,
        use=M,
        manage=S,
        verb_labels={
            "use": _("Station bedienen (Lock, PTT, Mikrofon)"),
            "manage": _("Station kalibrieren (Filter, Lautstärke, Audio-Diagnose)"),
        },
    ),
    "terminal": Area(_("Terminal"), use=AD, verb_labels={"use": _("Root-Shell öffnen")}),
    "topology": Area(
        _("Regionen & Zuweisungen"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Regionen und Zuweisungen verwalten")},
    ),
    "deployments": Area(
        _("Deployments"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Deployments auslösen/abbrechen")},
    ),
    "rollouts": Area(
        _("Rollouts"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Rollout-Sequenz bearbeiten, Gruppen-Upgrade")},
    ),
    "images": Area(
        _("Images"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Images importieren/archivieren/als Latest markieren")},
    ),
    "provisioning": Area(_("Provisioning"), manage=AD),
    "alerts": Area(
        _("Alarme"), view=A, manage=S, verb_labels={"manage": _("Alarme quittieren/auflösen")}
    ),
    "alert_rules": Area(
        _("Alarm-Regeln"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Alarm-Regeln und Benachrichtigungen konfigurieren")},
    ),
    "modules": Area(
        _("Module & Firmware"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Module bestätigen, Firmware importieren/archivieren")},
    ),
    "users": Area(
        _("Benutzer"), view=M, manage=AD, verb_labels={"manage": _("Benutzer verwalten")}
    ),
    "api_tokens": Area(_("API-Tokens"), use=M, verb_labels={"use": _("API-Tokens erstellen")}),
    "audit": Area(_("Audit-Log"), view=AD),
    "sso": Area(_("SSO-Verwaltung"), manage=AD),
}

_DEFAULT_VERB_LABELS = {"view": _("ansehen"), "use": _("bedienen"), "manage": _("verwalten")}


def _validate_areas(areas: dict[str, Area]) -> None:
    for name, area in areas.items():
        defined = [getattr(area, v) for v in VERBS if getattr(area, v) is not None]
        if not defined:
            raise ImproperlyConfigured(f"permissions: area {name!r} defines no verb")
        if defined != sorted(defined):
            raise ImproperlyConfigured(f"permissions: area {name!r} is not monotonic")
        for verb in area.verb_labels:
            if verb not in VERBS or getattr(area, verb) is None:
                raise ImproperlyConfigured(
                    f"permissions: area {name!r} labels undefined verb {verb!r}"
                )


_validate_areas(AREAS)


def required_level(area: str, verb: str) -> Level | None:
    """Resolved minimum level; ``None`` = nobody. Unknown area/verb → KeyError."""
    spec = AREAS[area]
    if verb not in VERBS:
        raise KeyError(verb)
    for candidate in VERBS[VERBS.index(verb) :]:
        level = getattr(spec, candidate)
        if level is not None:
            return level
    return None


def level_of(user) -> Level | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    if not getattr(user, "is_active", False) or getattr(user, "deleted_at", None) is not None:
        return None
    return Level.from_slug(getattr(user, "membership_level", None))


def level_label(level: Level | None) -> str:
    if level is None:
        return str(_("niemand"))
    from apps.accounts.models import User

    return str(User.MembershipLevel(level.slug).label)


def verb_label(area: str, verb: str) -> str:
    spec = AREAS[area]
    if verb in spec.verb_labels:
        return str(spec.verb_labels[verb])
    return f"{spec.label} – {_DEFAULT_VERB_LABELS[verb]}"


def can(user, area: str, verb: str) -> bool:
    needed = required_level(area, verb)  # KeyError on typos, even for anonymous
    have = level_of(user)
    return needed is not None and have is not None and have >= needed


def users_with(area: str, verb: str):
    from apps.accounts.models import User

    needed = required_level(area, verb)
    if needed is None:
        return User.objects.none()
    slugs = [lvl.slug for lvl in Level if lvl >= needed]
    return User.objects.filter(is_active=True, deleted_at__isnull=True, membership_level__in=slugs)


def can_override_station(user, station) -> bool:
    """The only assignment-based rule: preempt/override the control lock.

    ``control.manage`` OR Station-Admin of ``station`` OR Region-Manager of its
    region. Assignment holders additionally need ``control.use`` (preempting
    hands them the lock, which they must be allowed to operate).
    """
    if can(user, "control", "manage"):
        return True
    if not can(user, "control", "use"):
        return False
    if user.station_assignments.filter(station=station, role="admin").exists():
        return True
    region_id = getattr(station, "region_id", None)
    return (
        region_id is not None
        and user.region_assignments.filter(region_id=region_id, role="manager").exists()
    )


@dataclasses.dataclass(frozen=True)
class MatrixRow:
    area: str
    label: str
    view: Level | None
    use: Level | None
    manage: Level | None


def matrix() -> list[MatrixRow]:
    return [
        MatrixRow(
            area=name,
            label=str(spec.label),
            view=required_level(name, "view"),
            use=required_level(name, "use"),
            manage=required_level(name, "manage"),
        )
        for name, spec in AREAS.items()
    ]
