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
