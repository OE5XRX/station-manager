"""Tests for ApplicationPolicy model.

The model is a 1:1 sidecar to DOT's Application. Missing rows are
equivalent to GRANT_REQUIRED (the pre-existing behaviour). Stored
rows can express auto-approval policies tied to membership_level.
"""

import pytest
from oauth2_provider.models import Application

from apps.accounts.models import User
from apps.sso.models import AppGrant, ApplicationPolicy
from apps.sso.permissions import user_can_access


@pytest.fixture
def app(db):
    return Application.objects.create(
        name="InvenTree",
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://example.org/oidc/callback/",
    )


def test_application_policy_default_is_grant_required(db, app):
    policy = ApplicationPolicy.objects.create(application=app)
    assert policy.access_policy == ApplicationPolicy.AccessPolicy.GRANT_REQUIRED


def test_application_policy_choices_include_all_five(db):
    choices = {value for value, _ in ApplicationPolicy.AccessPolicy.choices}
    assert choices == {
        "grant_required",
        "open_to_all",
        "open_to_members",
        "open_to_internal",
        "open_to_admins",
    }


def test_application_policy_is_one_to_one_with_application(db, app):
    ApplicationPolicy.objects.create(application=app)
    with pytest.raises(Exception):  # IntegrityError; sqlite/pg-agnostic
        ApplicationPolicy.objects.create(application=app)


def test_application_policy_modified_by_is_optional(db, app):
    policy = ApplicationPolicy.objects.create(application=app)
    assert policy.modified_by is None


def _make_user(level: str, *, active: bool = True) -> User:
    u = User.objects.create_user(username=f"u-{level}-{int(active)}", password="x")
    u.membership_level = level
    u.is_active = active
    u.save(update_fields=["membership_level", "is_active"])
    User._invalidate_role_cache(u)
    return u


@pytest.mark.parametrize(
    "policy,level,is_active,has_grant,expected",
    [
        # GRANT_REQUIRED: existing behaviour, no policy row needed
        ("grant_required", "applicant", True, True, True),
        ("grant_required", "applicant", True, False, False),
        ("grant_required", "member", True, True, True),
        ("grant_required", "admin", False, True, False),
        # OPEN_TO_ALL: every active user, including applicants
        ("open_to_all", "applicant", True, False, True),
        ("open_to_all", "applicant", False, True, False),
        ("open_to_all", "member", True, False, True),
        # OPEN_TO_MEMBERS: applicant out, member+ in
        ("open_to_members", "applicant", True, False, False),
        ("open_to_members", "member", True, False, True),
        ("open_to_members", "staff", True, False, True),
        ("open_to_members", "admin", True, False, True),
        # OPEN_TO_INTERNAL: staff + admin
        ("open_to_internal", "member", True, False, False),
        ("open_to_internal", "staff", True, False, True),
        ("open_to_internal", "admin", True, False, True),
        # OPEN_TO_ADMINS: admin only
        ("open_to_admins", "staff", True, False, False),
        ("open_to_admins", "admin", True, False, True),
        # GRANT_REQUIRED additional negatives (no policy row + no grant)
        ("grant_required", "staff", True, False, False),
        ("grant_required", "admin", True, False, False),
        # OPEN_TO_INTERNAL applicant boundary (denied)
        ("open_to_internal", "applicant", True, False, False),
        # OPEN_TO_ADMINS member boundary (denied)
        ("open_to_admins", "member", True, False, False),
    ],
)
def test_user_can_access_matrix(db, app, policy, level, is_active, has_grant, expected):
    user = _make_user(level, active=is_active)
    if policy != "grant_required":
        ApplicationPolicy.objects.create(application=app, access_policy=policy)
    if has_grant:
        AppGrant.objects.create(user=user, application=app)

    assert user_can_access(user, app) is expected


def test_inactive_user_never_allowed_even_with_open_to_all(db, app):
    user = _make_user("admin", active=False)
    ApplicationPolicy.objects.create(application=app, access_policy="open_to_all")
    assert user_can_access(user, app) is False
