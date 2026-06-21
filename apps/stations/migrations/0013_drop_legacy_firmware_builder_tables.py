from django.db import migrations

# Die firmware/builder-Tabellen sind in Prod verifiziert leer (2026-06-21) und
# die Apps sind entfernt. Wir droppen die orphaned Tabellen + die zugehörigen
# django_migrations-Zeilen. firmware VOR stations_moduletype-Drop (Migration 0014),
# weil firmware_firmwareartifact.target_module einen FK auf stations_moduletype hält
# (daher CASCADE auf Postgres). Auf frischen DBs (SQLite-Test/CI) sind diese Tabellen
# nie entstanden -> reiner No-op. CASCADE ist kein gültiges SQLite-Syntax, deshalb
# läuft die SQL nur auf PostgreSQL.
LEGACY_DROP_STATEMENTS = [
    "DROP TABLE IF EXISTS builder_buildjob CASCADE",
    "DROP TABLE IF EXISTS builder_buildconfig CASCADE",
    "DROP TABLE IF EXISTS firmware_firmwaredelta CASCADE",
    "DROP TABLE IF EXISTS firmware_firmwareartifact CASCADE",
    "DELETE FROM django_migrations WHERE app IN ('firmware', 'builder')",
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
