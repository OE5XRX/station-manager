"""Verify the data migration that maps User.role to auth.Group memberships.

We use django_test_migrations so the migration runs against a clean
schema and we can assert behavior at the boundary between 0001 and 0002.
"""

import pytest


@pytest.mark.django_db(transaction=True)
def test_role_to_groups_migration_assigns_users_to_correct_groups(migrator):
    """Each existing user lands in exactly one group matching their old role."""
    old_state = migrator.apply_initial_migration(
        [("accounts", "0001_initial")]
    )
    OldUser = old_state.apps.get_model("accounts", "User")
    OldUser.objects.create_user(
        username="alice", password="x", role="admin", email="a@x"
    )
    OldUser.objects.create_user(
        username="bob", password="x", role="operator", email="b@x"
    )
    OldUser.objects.create_user(
        username="carol", password="x", role="member", email="c@x"
    )

    new_state = migrator.apply_tested_migration(
        [("accounts", "0002_role_to_groups")]
    )
    NewUser = new_state.apps.get_model("accounts", "User")
    Group = new_state.apps.get_model("auth", "Group")

    assert {g.name for g in Group.objects.all()} >= {"admin", "operator", "member"}

    alice = NewUser.objects.get(username="alice")
    bob = NewUser.objects.get(username="bob")
    carol = NewUser.objects.get(username="carol")

    assert list(alice.groups.values_list("name", flat=True)) == ["admin"]
    assert list(bob.groups.values_list("name", flat=True)) == ["operator"]
    assert list(carol.groups.values_list("name", flat=True)) == ["member"]


@pytest.mark.django_db(transaction=True)
def test_groups_exist_even_with_no_users(migrator):
    """The three default groups are created idempotently with zero users."""
    migrator.apply_initial_migration([("accounts", "0001_initial")])
    new_state = migrator.apply_tested_migration(
        [("accounts", "0002_role_to_groups")]
    )
    Group = new_state.apps.get_model("auth", "Group")
    assert {g.name for g in Group.objects.all()} >= {"admin", "operator", "member"}
