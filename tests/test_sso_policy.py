"""Tests for ApplicationPolicy model.

The model is a 1:1 sidecar to DOT's Application. Missing rows are
equivalent to GRANT_REQUIRED (the pre-existing behaviour). Stored
rows can express auto-approval policies tied to membership_level.
"""

import pytest
from oauth2_provider.models import Application

from apps.sso.models import ApplicationPolicy


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
