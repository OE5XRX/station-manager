"""The one place that defines who may do what.

Model (spec 2026-10-14-unified-permissions-design.md): every capability is an
*area* × one of three fixed verbs (view ⊆ use ⊆ manage), each mapped to a
minimum membership level. UI views, templates, the REST API and WebSockets all
read from here; nothing else compares membership levels for authorization.

The single assignment-based exception is :func:`can_override_station`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
from enum import IntEnum

from channels.db import database_sync_to_async
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext_lazy as _
from rest_framework.permissions import BasePermission

logger = logging.getLogger(__name__)

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
    "profile": Area(_("Own profile"), use=A),
    "stations": Area(
        _("Stations"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Create/edit stations, logbook, photos, station audit")},
    ),
    "control": Area(
        _("Station control"),
        view=A,
        use=M,
        manage=S,
        verb_labels={
            "use": _("Operate a station (lock, PTT, microphone)"),
            "manage": _("Calibrate a station (filters, volume, audio diagnostics)"),
        },
    ),
    "terminal": Area(_("Terminal"), use=AD, verb_labels={"use": _("Open a root shell")}),
    "topology": Area(
        _("Regions & assignments"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Manage regions and assignments")},
    ),
    "deployments": Area(
        _("Deployments"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Trigger/cancel deployments")},
    ),
    "rollouts": Area(
        _("Rollouts"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Edit rollout sequence, group upgrade")},
    ),
    "images": Area(
        _("Images"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Import/archive images, mark as latest")},
    ),
    "provisioning": Area(_("Provisioning"), manage=AD),
    "alerts": Area(
        _("Alerts"), view=A, manage=S, verb_labels={"manage": _("Acknowledge/resolve alerts")}
    ),
    "alert_rules": Area(
        _("Alert rules"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Configure alert rules and notifications")},
    ),
    "modules": Area(
        _("Modules & firmware"),
        view=A,
        manage=S,
        verb_labels={
            "manage": _(
                "Confirm modules, import/archive firmware, module types, "
                "target versions, release quarantine"
            )
        },
    ),
    "repeaters": Area(
        _("Repeaters"),
        view=A,
        manage=S,
        verb_labels={"manage": _("Create/edit repeaters")},
    ),
    "passive_services": Area(
        _("Passive services"),
        view=A,
        manage=AD,
        verb_labels={"manage": _("Manage passive services")},
    ),
    "users": Area(_("Users"), view=M, manage=AD, verb_labels={"manage": _("Manage users")}),
    "api_tokens": Area(_("API tokens"), use=M, verb_labels={"use": _("Create API tokens")}),
    "audit": Area(_("Audit log"), view=AD),
    "sso": Area(_("SSO administration"), manage=AD),
}

_DEFAULT_VERB_LABELS = {"view": _("view"), "use": _("operate"), "manage": _("manage")}


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
        return str(_("nobody"))
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


DENIAL_DEDUPE_SECONDS = 600


class AreaDenied(PermissionDenied):
    def __init__(self, area: str, verb: str, actual_level: Level | None):
        self.area = area
        self.verb = verb
        self.required_level = required_level(area, verb)
        self.actual_level = actual_level
        super().__init__(self._message())

    def _message(self) -> str:
        what = verb_label(self.area, self.verb)
        if self.required_level is None:
            return str(
                _("Permission denied — %(what)s is not available to anyone.") % {"what": what}
            )
        return str(
            _(
                "Permission denied — %(what)s requires %(need)s, you are %(have)s. "
                "If you think this is wrong, contact an admin."
            )
            % {
                "what": what,
                "need": level_label(self.required_level),
                "have": level_label(self.actual_level),
            }
        )

    def as_api_body(self) -> dict:
        return {
            "detail": str(self),
            "required": f"{self.area}.{self.verb}",
            "required_level": self.required_level.slug if self.required_level else None,
            "your_level": self.actual_level.slug if self.actual_level else None,
        }


def _client_ip(request):
    """Trusted client IP for web/API denials (see apps.accounts.client_ip)."""
    from apps.accounts.client_ip import client_ip

    return client_ip(request)


def _ws_client_ip(scope) -> str | None:
    from apps.accounts.client_ip import ws_client_ip

    return ws_client_ip(scope)


def log_denial(user, exc: AreaDenied, *, channel: str, path: str, method: str, ip=None) -> None:
    from apps.accounts.models import AccountAuditLog

    if not getattr(user, "is_authenticated", False):
        return  # anonymous: never logged
    # Hash the user-controlled path: raw URLs can hold spaces/control chars or
    # exceed cache-key limits (CacheKeyWarning / InvalidCacheKey -> 500).
    path_digest = hashlib.sha256(path.encode()).hexdigest()
    key = f"perm-denied:{user.pk}:{exc.area}:{exc.verb}:{path_digest}"
    path = path[:255]
    pk = cache.get(key)
    if pk is not None:
        with transaction.atomic():
            row = AccountAuditLog.objects.select_for_update().filter(pk=pk).first()
            if row is not None:
                row.details["count"] = int(row.details.get("count", 1)) + 1
                row.save(update_fields=["details"])
                return
    row = AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.PERMISSION_DENIED,
        actor=user,
        message=f"{exc.area}.{exc.verb} denied ({method} {path})",
        ip_address=ip,
        details={
            "area": exc.area,
            "verb": exc.verb,
            "path": path,
            "method": method,
            "channel": channel,
            "required_level": exc.required_level.slug if exc.required_level else None,
            "actual_level": exc.actual_level.slug if exc.actual_level else None,
            "count": 1,
        },
    )
    cache.set(key, row.pk, DENIAL_DEDUPE_SECONDS)


def require(user, area, verb, *, request=None, channel="web", path=None, method=None, ip=None):
    if can(user, area, verb):
        return
    exc = AreaDenied(area, verb, level_of(user))
    if getattr(user, "is_authenticated", False):
        log_denial(
            user,
            exc,
            channel=channel,
            path=path if path is not None else (request.path if request is not None else ""),
            method=method
            if method is not None
            else (request.method if request is not None else ""),
            ip=ip if ip is not None else _client_ip(request),
        )
    raise exc


SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def resolve_verb(view, method: str) -> str:
    method = method.upper()
    verbs = getattr(view, "verbs", None) or {}
    if method in verbs:
        return verbs[method]
    explicit = getattr(view, "verb", None)
    if explicit:
        return explicit
    return "view" if method in SAFE_METHODS else "manage"


class AreaPermissionMixin:
    """Leftmost base of every Django CBV. Declare ``area`` (+ optional ``verb``/``verbs``)."""

    area: str | None = None
    verb: str | None = None
    verbs: dict[str, str] | None = None

    def get_area_verb(self) -> tuple[str, str]:
        return self.area, resolve_verb(self, self.request.method)

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            from django.contrib.auth.views import redirect_to_login

            return redirect_to_login(request.get_full_path())
        # ``require`` runs before ``View.dispatch``: a method the view does not
        # implement must answer 405, not map to ``manage`` -> 403 + a false
        # PERMISSION_DENIED audit row.
        handler_name = request.method.lower()
        if handler_name not in self.http_method_names or not hasattr(self, handler_name):
            return self.http_method_not_allowed(request, *args, **kwargs)
        area, verb = self.get_area_verb()
        require(request.user, area, verb, request=request, channel="web")
        return super().dispatch(request, *args, **kwargs)


def area_permission(area: str, verb: str | None = None):
    import functools

    def decorator(func):
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                from django.contrib.auth.views import redirect_to_login

                return redirect_to_login(request.get_full_path())
            used = verb or ("view" if request.method.upper() in SAFE_METHODS else "manage")
            require(request.user, area, used, request=request, channel="web")
            return func(request, *args, **kwargs)

        wrapper.area = area
        wrapper.verb = verb
        wrapper.area_enforced = True
        return wrapper

    return decorator


def permission_exempt(reason: str):
    if not reason or not reason.strip():
        raise ImproperlyConfigured("permission_exempt needs a non-empty reason")

    def decorator(func):
        func.permission_exempt = reason
        return func

    return decorator


class AreaPermission(BasePermission):
    """DRF counterpart of :class:`AreaPermissionMixin`.

    Non-user principals (DeviceKey, anonymous) are refused without an audit
    entry; users go through :func:`require` (raises ``AreaDenied`` -> JSON 403).
    """

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        if not getattr(user, "is_authenticated", False) or not hasattr(user, "membership_level"):
            return False
        verb = resolve_verb(view, request.method)
        # DRF probes other methods via ``override_method``/``clone_request``:
        # OPTIONS metadata (SimpleMetadata.determine_actions) and the browsable
        # API's form rendering (BrowsableAPIRenderer.show_form_for_method) both
        # clone the request as POST/PUT and re-run check_permissions, while the
        # underlying HttpRequest keeps the real method. Such a probe is not a
        # real attempt: answer it silently instead of raising (-> 403 on a
        # plain GET) and writing a false PERMISSION_DENIED audit row.
        original = getattr(request, "_request", None)
        if original is not None and request.method.upper() != original.method.upper():
            return can(user, view.area, verb)
        # A method the view does not implement: let DRF answer 405 instead of
        # a 403 + false PERMISSION_DENIED row (``manage`` is the fallback verb).
        allowed = getattr(view, "allowed_methods", None)
        if allowed is not None and request.method.upper() not in allowed:
            return True
        require(user, view.area, verb, request=request, channel="api")
        return True

    def has_object_permission(self, request, view, obj):
        return True


def ws_denied_frame(exc: AreaDenied, request_id=None) -> dict:
    frame = {"type": "error", "code": "forbidden", "message": str(exc)}
    if request_id is not None:
        frame["request_id"] = request_id
    return frame


def _load_fresh_user(pk):
    from apps.accounts.models import User

    if pk is None:
        return None
    return User.objects.filter(pk=pk).first()


#: How often passive feeds re-check ``connect_verb`` (see
#: :meth:`AreaConsumerMixin.start_periodic_recheck`). Read at runtime, so tests
#: can monkeypatch it.
PERIODIC_RECHECK_SECONDS = 60.0
#: Consecutive failed re-checks (e.g. DB down) after which the feed is closed
#: (1011) instead of staying open unchecked.
RECHECK_MAX_FAILURES = 2


class AreaConsumerMixin:
    """Channels consumer gate: re-reads the user from the DB on every check."""

    area: str | None = None
    connect_verb: str = "view"
    _recheck_task: asyncio.Task | None = None

    async def dispatch(self, message):
        # WS frames carry no locale prefix: user-visible texts (denied frames,
        # error messages) follow the user's User.language. asyncio tasks spawned
        # while handling a frame (rechecks, timeouts) inherit the context.
        from apps.accounts.language import is_supported_language

        user = self.scope.get("user")
        language = getattr(user, "language", None) if user and user.is_authenticated else None
        if not is_supported_language(language):
            await super().dispatch(message)
            return
        with translation.override(language):
            await super().dispatch(message)

    async def fresh_user(self):
        # database_sync_to_async closes stale/broken DB connections around the call.
        user = self.scope.get("user")
        return await database_sync_to_async(_load_fresh_user)(getattr(user, "pk", None))

    def start_periodic_recheck(self) -> None:
        """Opt-in for passive feeds: re-check ``connect_verb`` every
        ``PERIODIC_RECHECK_SECONDS`` with a fresh user. A listener never sends a
        frame, so per-frame checks alone would let a deactivated/demoted viewer
        keep listening until reconnect. Call after the connect gate passed; pair
        with :meth:`stop_periodic_recheck` in ``disconnect``."""
        if self._recheck_task is None:
            self._recheck_task = asyncio.ensure_future(self._periodic_recheck_loop())

    async def stop_periodic_recheck(self) -> None:
        task, self._recheck_task = self._recheck_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def on_periodic_recheck(self, user) -> None:
        """Hook: refresh permission-derived state (still allowed; ``user`` is fresh)."""

    async def _periodic_recheck_loop(self) -> None:
        failures = 0
        while True:
            await asyncio.sleep(PERIODIC_RECHECK_SECONDS)
            try:
                user = await self.fresh_user()
                allowed = await self.area_allows(self.connect_verb, user=user)
                if allowed:
                    await self.on_periodic_recheck(user)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception(
                    "permissions: periodic WS re-check failed (%d/%d)",
                    failures,
                    RECHECK_MAX_FAILURES,
                )
                if failures < RECHECK_MAX_FAILURES:
                    continue  # retry next tick
                # Never leave a feed open without a working permission check.
                self._recheck_task = None
                try:
                    await self.send(
                        text_data=json.dumps(
                            {
                                "type": "error",
                                "code": "recheck_failed",
                                "message": str(_("Permission could not be checked.")),
                            }
                        )
                    )
                finally:
                    await self.close(code=1011)
                return
            failures = 0
            if not allowed:
                # One denial (audited once), then the feed ends: no further checks.
                self._recheck_task = None
                await self.close(code=4403)
                return

    def _check(self, user, area, verb):
        require(
            user,
            area,
            verb,
            channel="ws",
            path=self.scope.get("path", ""),
            method=f"WS {verb}",
            ip=_ws_client_ip(self.scope),
        )

    async def area_allows(
        self, verb, *, area=None, request_id=None, notify=True, user=None
    ) -> bool:
        """``user``: an already freshly loaded user (default: load one now)."""
        from django.contrib.auth.models import AnonymousUser

        if user is None:
            user = await self.fresh_user()
        user = user or AnonymousUser()
        try:
            await database_sync_to_async(self._check)(user, area or self.area, verb)
        except AreaDenied as exc:
            if notify:
                await self.send(text_data=json.dumps(ws_denied_frame(exc, request_id)))
            return False
        return True

    async def area_connect_gate(self) -> bool:
        """Call right after ``accept()``; closes with 4403 when denied."""
        if await self.area_allows(self.connect_verb, notify=True):
            return True
        await self.close(code=4403)
        return False


# Third-party / framework URL trees that cannot carry our declarations.
# Keep these to third-party code only; own views declare ``permission_exempt``.
# Names are matched against the full namespace path ("ns:sub:name").
EXEMPT_URL_NAMESPACES: dict[str, str] = {
    "admin": "django-admin: gated by derived is_staff",
    "djdt": "debug-toolbar: DEBUG only",
}
_OIDC = "oidc: protocol endpoint"
EXEMPT_URL_NAMES: dict[str, str] = {
    "set_language": "locale switch",
    "javascript-catalog": "i18n: public JS translation catalog, no data",
    "api:schema": "api-docs: public OpenAPI schema, no data",
    "api:docs": "api-docs: public OpenAPI schema, no data",
    **{
        f"oauth2_provider:{name}": _OIDC
        for name in (
            "oauth-server-metadata",
            "oauth-server-metadata-issuer",
            "oauth-resource-metadata",
            "oauth-resource-metadata-path",
            "authorize",
            "token",
            "revoke-token",
            "introspect",
            "oidc-connect-discovery-info",
            "jwks-info",
            "user-info",
            "rp-initiated-logout",
        )
    },
}
