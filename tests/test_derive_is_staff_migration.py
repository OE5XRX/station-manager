"""Migration 0023 derives is_staff/is_superuser from the membership level."""

import pytest


@pytest.mark.django_db(transaction=True)
def test_0023_derives_is_staff_from_level(migrator):
    old_state = migrator.apply_initial_migration(
        [("accounts", "0022_accountauditlog_details_permission_denied")]
    )
    User = old_state.apps.get_model("accounts", "User")
    User.objects.create(username="m", membership_level="member", is_staff=True, is_superuser=True)
    User.objects.create(username="a", membership_level="admin", is_staff=False)
    User.objects.create(username="s", membership_level="staff", is_staff=True, is_superuser=True)
    User.objects.create(
        username="p", membership_level="applicant", is_staff=True, is_superuser=True
    )

    new_state = migrator.apply_tested_migration([("accounts", "0023_derive_is_staff")])
    User = new_state.apps.get_model("accounts", "User")
    m = User.objects.get(username="m")
    a = User.objects.get(username="a")
    assert (m.is_staff, m.is_superuser) == (False, False)
    assert (a.is_staff, a.is_superuser) == (True, True)
    for name in ("s", "p"):
        u = User.objects.get(username=name)
        assert (u.is_staff, u.is_superuser) == (False, False), name
