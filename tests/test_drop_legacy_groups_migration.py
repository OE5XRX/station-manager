"""Verify migration 0007 deletes the legacy role-groups and that the
reverse-code re-creates them for rollback safety."""

import pytest


@pytest.mark.django_db(transaction=True)
def test_0007_drops_legacy_groups(migrator):
    old_state = migrator.apply_initial_migration([("accounts", "0006_add_account_audit_log")])
    Group = old_state.apps.get_model("auth", "Group")
    for name in ("admin", "operator", "member"):
        Group.objects.get_or_create(name=name)
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 3

    new_state = migrator.apply_tested_migration([("accounts", "0007_drop_legacy_role_groups")])
    Group = new_state.apps.get_model("auth", "Group")
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 0


@pytest.mark.django_db(transaction=True)
def test_0007_reverse_recreates_groups(migrator):
    old_state = migrator.apply_initial_migration([("accounts", "0006_add_account_audit_log")])
    Group = old_state.apps.get_model("auth", "Group")
    for name in ("admin", "operator", "member"):
        Group.objects.get_or_create(name=name)

    new_state = migrator.apply_tested_migration([("accounts", "0007_drop_legacy_role_groups")])
    Group = new_state.apps.get_model("auth", "Group")
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 0

    # Reverse to 0006
    final_state = migrator.apply_tested_migration([("accounts", "0006_add_account_audit_log")])
    Group = final_state.apps.get_model("auth", "Group")
    assert Group.objects.filter(name__in=["admin", "operator", "member"]).count() == 3
