"""Guard tests: legacy permission helpers must not come back."""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCAN_DIRS = ["apps", "config", "templates"]
SCAN_GLOBS = ["station_manager*"]
FORBIDDEN = [
    r"\bis_admin\b",
    r"\bis_internal\b",
    r"\bis_staff\b",
    r"\bis_superuser\b",
    r"\bcan_use_station\b",
    r"\bcan_maintain_station\b",
    r"\b_invalidate_role_cache\b",
    r"\bAdminRequiredMixin\b",
    r"\bAdminOrOperatorRequiredMixin\b",
    r"\bAdminOnlyMixin\b",
    r"\b_StaffModuleMixin\b",
    r"\b_StaffFirmwareMixin\b",
    r"\bwrite_scoping\b",
    r"\bwrite_permissions\b",
    r"\baccessible_stations\b",
    r"\bTopologyScopedPermission\b",
    r"\bROLE_RANK\b",
    r"\bROLE_LABELS\b",
    r"\bviewer_role\b",
    r"\brole_allows\b",
]
ALLOW = {
    "apps/accounts/permissions.py",
    "apps/accounts/models.py",
    "apps/accounts/managers.py",
    "apps/accounts/admin.py",
}
LEVEL_COMPARISON = re.compile(
    r"membership_level\s*[!=]=|MembershipLevel\.\w+\s*[!=]=|[!=]=\s*[\w.]*MembershipLevel\."
)
LEVEL_ALLOW = {
    "apps/accounts/permissions.py",
    "apps/accounts/models.py",
    "apps/accounts/views_membership.py",
    "apps/api/write_serializers.py",
}


def _files():
    for d in SCAN_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.suffix in {".py", ".html", ".txt", ".js"} and "migrations" not in p.parts:
                yield p
    for g in SCAN_GLOBS:
        for base in ROOT.glob(g):
            for p in base.rglob("*.py") if base.is_dir() else [base]:
                yield p


def test_no_forbidden_permission_names():
    hits = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if rel in ALLOW:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            for pat in FORBIDDEN:
                if re.search(pat, line):
                    hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "Old permission helpers used:\n" + "\n".join(hits)


def test_no_direct_membership_level_comparisons():
    hits = []
    for p in _files():
        rel = p.relative_to(ROOT).as_posix()
        if p.suffix != ".py" or rel in LEVEL_ALLOW:
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if LEVEL_COMPARISON.search(line):
                hits.append(f"{rel}:{n}: {line.strip()}")
    assert not hits, "Compare levels via apps.accounts.permissions:\n" + "\n".join(hits)


def test_user_model_has_no_legacy_role_properties():
    text = (ROOT / "apps/accounts/models.py").read_text()
    assert "def is_admin" not in text and "def is_internal" not in text


@pytest.mark.django_db
@pytest.mark.parametrize(
    "level,expected",
    [("applicant", False), ("member", False), ("staff", False), ("admin", True)],
)
def test_is_staff_derived_from_level(level, expected):
    from apps.accounts.models import User

    u = User.objects.create_user(
        username=f"d-{level}", password="x", membership_level=level, is_staff=True
    )
    u.refresh_from_db()
    assert (u.is_staff, u.is_superuser) == (expected, expected)


@pytest.mark.django_db
def test_demotion_with_update_fields_clears_is_staff():
    from apps.accounts.models import User

    u = User.objects.create_user(username="dd", password="x", membership_level="admin")
    u.membership_level = "member"
    u.save(update_fields=["membership_level"])
    u.refresh_from_db()
    assert u.is_staff is False


@pytest.mark.django_db
def test_createsuperuser_is_admin_level():
    from apps.accounts.models import User

    u = User.objects.create_superuser("root", "r@example.org", "pw")
    assert u.membership_level == "admin" and u.is_staff and u.is_superuser


# --- Task 14: URL-walk, consumer and template-reference guards ---------------


def _walk(patterns, ns=None):
    """Yield (full namespace path, pattern); nested namespaces are joined by ':'."""
    from django.urls import URLPattern, URLResolver

    for p in patterns:
        if isinstance(p, URLResolver):
            sub = ":".join(x for x in (ns, p.namespace) if x) or None
            yield from _walk(p.url_patterns, sub)
        elif isinstance(p, URLPattern):
            yield ns, p


def _declaration(callback):
    view = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    target = view or callback
    initkwargs = getattr(callback, "initkwargs", {}) or {}
    area = initkwargs.get("area") or getattr(target, "area", None)
    exempt = getattr(target, "permission_exempt", None)
    return view, target, area, exempt


def test_every_url_declares_area_or_exemption():
    from django.urls import get_resolver

    from apps.accounts import permissions as perms

    for table in (perms.EXEMPT_URL_NAMESPACES, perms.EXEMPT_URL_NAMES):
        assert all(str(r).strip() for r in table.values())

    problems = []
    for ns, pat in _walk(get_resolver().url_patterns):
        full_name = f"{ns}:{pat.name}" if ns and pat.name else pat.name
        if (ns or "").split(":")[
            0
        ] in perms.EXEMPT_URL_NAMESPACES or full_name in perms.EXEMPT_URL_NAMES:
            continue
        view, target, area, exempt = _declaration(pat.callback)
        if exempt:
            assert str(exempt).strip(), full_name
            continue
        label = full_name or str(pat.pattern)
        if not area:
            problems.append(f"{label}: no area/permission_exempt ({target})")
            continue
        assert area in perms.AREAS, f"{label}: unknown area {area!r}"
        enforced = (
            (view is not None and issubclass(view, perms.AreaPermissionMixin))
            or (
                view is not None
                and perms.AreaPermission in getattr(view, "permission_classes", [])
            )
            or getattr(target, "area_enforced", False)
        )
        if not enforced:
            problems.append(f"{label}: declares area but does not enforce it")
        verb = getattr(target, "verb", None)
        assert verb in (None, *perms.VERBS), f"{label}: bad verb {verb!r}"
        for method, mverb in (getattr(target, "verbs", None) or {}).items():
            assert mverb in perms.VERBS, f"{label}: bad verbs[{method!r}] = {mverb!r}"
    assert not problems, "\n".join(problems)


def test_every_consumer_declares_area_or_exemption():
    import importlib
    import inspect

    from apps.accounts import permissions as perms
    from config.ws_routes import ROUTING_MODULES, agent_ws_routes, browser_ws_routes

    on_disk = {p.parent.name for p in (ROOT / "apps").glob("*/routing.py")}
    included = {m.__name__.split(".")[1] for m in ROUTING_MODULES}
    assert on_disk == included, f"routing modules not in config/ws_routes.py: {on_disk ^ included}"
    for name in on_disk:
        importlib.import_module(f"apps.{name}.routing")

    for route in [*browser_ws_routes, *agent_ws_routes]:
        consumer = route.callback.consumer_class
        area = getattr(consumer, "area", None)
        exempt = getattr(consumer, "permission_exempt", None)
        assert area or exempt, consumer
        if exempt:
            assert str(exempt).strip(), consumer
        if area:
            assert area in perms.AREAS and issubclass(consumer, perms.AreaConsumerMixin), consumer
            assert "area_connect_gate" in inspect.getsource(consumer.connect), (
                f"{consumer.__name__}.connect does not call area_connect_gate"
            )
    for route in browser_ws_routes:
        assert getattr(route.callback.consumer_class, "area", None), (
            "browser consumers need an area"
        )


TEMPLATE_CAN = re.compile(r"\bcan\.(\w+)\.(\w+)")


def test_template_can_references_are_valid():
    from apps.accounts.permissions import AREAS, VERBS

    bad = []
    for d in ["apps", "templates"]:
        for p in (ROOT / d).rglob("*.html"):
            for m in TEMPLATE_CAN.finditer(p.read_text(encoding="utf-8")):
                if m.group(1) not in AREAS or m.group(2) not in VERBS:
                    bad.append(f"{p.relative_to(ROOT)}: {m.group(0)}")
    assert not bad, "\n".join(bad)


def test_every_drf_action_verb_is_valid():
    from apps.accounts.permissions import VERBS
    from apps.api.router import router

    checked = 0
    for _prefix, viewset, basename in router.registry:
        for name in dir(viewset):
            fn = getattr(viewset, name, None)
            kwargs = getattr(fn, "kwargs", None)
            if getattr(fn, "mapping", None) is not None and kwargs and "verb" in kwargs:
                assert kwargs["verb"] in VERBS, (basename, name)
                checked += 1
    assert checked, "expected at least one @action with an explicit verb"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/sso/applications/", "/sso/applications/register/", "/sso/authorized_tokens/"]
)
def test_dot_management_views_are_not_mounted(client, django_user_model, path):
    user = django_user_model.objects.create_user(username="plain", password="x", email="p@x.org")
    client.force_login(user)
    assert client.get(path).status_code in (403, 404)


@pytest.mark.parametrize(
    "name", ["device-authorization", "device", "device-confirm", "device-grant-status"]
)
def test_dot_device_flow_is_not_mounted(name):
    """RFC 8628 device grant bypasses the per-app SSO policy and is unused."""
    from django.urls import NoReverseMatch, reverse

    from apps.accounts.permissions import EXEMPT_URL_NAMES

    kwargs = {"client_id": "c", "user_code": "u"} if name.endswith(("confirm", "status")) else {}
    with pytest.raises(NoReverseMatch):
        reverse(f"oauth2_provider:{name}", kwargs=kwargs)
    assert f"oauth2_provider:{name}" not in EXEMPT_URL_NAMES


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/sso/device-authorization/", "/sso/device/", "/sso/device-confirm/c/u"]
)
def test_dot_device_flow_paths_404(client, django_user_model, path):
    user = django_user_model.objects.create_user(username="plain2", password="x", email="q@x.org")
    client.force_login(user)
    assert client.get(path).status_code == 404


@pytest.mark.django_db
def test_server_metadata_omits_device_endpoint(client):
    r = client.get("/sso/.well-known/oauth-authorization-server")
    assert r.status_code == 200
    assert "device_authorization_endpoint" not in r.json()
