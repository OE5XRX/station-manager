from django.db import migrations

# The firmware/builder tables are verified empty in prod (2026-06-21) and the
# apps are removed. We drop the orphaned tables. firmware BEFORE the
# stations_moduletype drop (migration 0014), because firmware_firmwareartifact.
# target_module holds an FK to stations_moduletype (hence CASCADE on Postgres).
# On fresh DBs (SQLite test/CI) these tables never existed -> pure no-op. CASCADE
# is not valid SQLite syntax, so the SQL runs on PostgreSQL only.
#
# We intentionally leave the corresponding django_migrations rows in place:
# Django ignores migration records for uninstalled apps, so they are inert. A
# DELETE on Django's own bookkeeping table from within a migration would be
# non-standard and can interfere with migrate --fake / squash / migration
# linters — dropping the tables is sufficient for the cleanup goal.
LEGACY_DROP_STATEMENTS = [
    # Implicit M2M join table from BuildConfig.extra_firmware. DROP ... CASCADE on
    # the parent table only drops its FK constraint, not the join table itself ->
    # drop it explicitly, otherwise it would be left behind as an orphan.
    "DROP TABLE IF EXISTS builder_buildconfig_extra_firmware CASCADE",
    "DROP TABLE IF EXISTS builder_buildjob CASCADE",
    "DROP TABLE IF EXISTS builder_buildconfig CASCADE",
    "DROP TABLE IF EXISTS firmware_firmwaredelta CASCADE",
    "DROP TABLE IF EXISTS firmware_firmwareartifact CASCADE",
]


def drop_legacy_tables(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return  # fresh DBs (SQLite test/CI) never had these tables
    with schema_editor.connection.cursor() as cursor:
        for statement in LEGACY_DROP_STATEMENTS:
            cursor.execute(statement)


class Migration(migrations.Migration):

    dependencies = [
        ("stations", "0012_extend_station_audit_event_types"),
    ]

    operations = [
        migrations.RunPython(drop_legacy_tables, migrations.RunPython.noop),
    ]
