"""Verify the Group → membership_level mapping in migration 0005.

The test uses django-test-migrations to drive migrations to a target
state, mutate data at the historical-model level, then migrate forward
and check the data.
"""

import pytest


@pytest.mark.django_db(transaction=True)
def test_0005_admin_group_maps_to_admin_level(migrator):
    old_state = migrator.apply_initial_migration([("accounts", "0004_add_membership_level")])
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    admin_group, _ = Group.objects.get_or_create(name="admin")
    u = User.objects.create_user(username="alice", password="x")
    u.groups.add(admin_group)

    new_state = migrator.apply_tested_migration([("accounts", "0005_seed_membership_levels")])
    User = new_state.apps.get_model("accounts", "User")
    alice = User.objects.get(username="alice")
    assert alice.membership_level == "admin"


@pytest.mark.django_db(transaction=True)
def test_0005_operator_group_maps_to_staff_level(migrator):
    old_state = migrator.apply_initial_migration([("accounts", "0004_add_membership_level")])
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    op_group, _ = Group.objects.get_or_create(name="operator")
    u = User.objects.create_user(username="bob", password="x")
    u.groups.add(op_group)

    new_state = migrator.apply_tested_migration([("accounts", "0005_seed_membership_levels")])
    User = new_state.apps.get_model("accounts", "User")
    assert User.objects.get(username="bob").membership_level == "staff"


@pytest.mark.django_db(transaction=True)
def test_0005_member_group_maps_to_member_level(migrator):
    old_state = migrator.apply_initial_migration([("accounts", "0004_add_membership_level")])
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    m_group, _ = Group.objects.get_or_create(name="member")
    u = User.objects.create_user(username="carol", password="x")
    u.groups.add(m_group)

    new_state = migrator.apply_tested_migration([("accounts", "0005_seed_membership_levels")])
    User = new_state.apps.get_model("accounts", "User")
    assert User.objects.get(username="carol").membership_level == "member"


@pytest.mark.django_db(transaction=True)
def test_0005_user_with_no_group_stays_applicant(migrator):
    old_state = migrator.apply_initial_migration([("accounts", "0004_add_membership_level")])
    User = old_state.apps.get_model("accounts", "User")
    User.objects.create_user(username="dave", password="x")

    new_state = migrator.apply_tested_migration([("accounts", "0005_seed_membership_levels")])
    User = new_state.apps.get_model("accounts", "User")
    # Default from 0004 stays APPLICANT
    assert User.objects.get(username="dave").membership_level == "applicant"


@pytest.mark.django_db(transaction=True)
def test_0005_user_in_multiple_groups_takes_highest(migrator):
    """Admin > Staff (operator) > Member precedence."""
    old_state = migrator.apply_initial_migration([("accounts", "0004_add_membership_level")])
    User = old_state.apps.get_model("accounts", "User")
    Group = old_state.apps.get_model("auth", "Group")
    a, _ = Group.objects.get_or_create(name="admin")
    o, _ = Group.objects.get_or_create(name="operator")
    m, _ = Group.objects.get_or_create(name="member")
    u = User.objects.create_user(username="eve", password="x")
    u.groups.add(a, o, m)

    new_state = migrator.apply_tested_migration([("accounts", "0005_seed_membership_levels")])
    User = new_state.apps.get_model("accounts", "User")
    assert User.objects.get(username="eve").membership_level == "admin"
