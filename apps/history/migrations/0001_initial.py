"""History tables — vendor-switched.

State: three plain ``CreateModel`` ops (so the autodetector sees the models as-is).
Database: one ``RunPython`` that creates the tables itself:

* SQLite (tests/dev): ``schema_editor.create_model`` — ordinary tables.
* PostgreSQL: ``HistoryState`` via ``create_model``; ``MetricSample``/``MetricHourly`` via
  hand-written DDL as monthly RANGE-partitioned tables on ``ts`` (PK must contain the
  partition key → composite PK ``(station_id, metric_key, ts)``), plus partitions for the
  current UTC month and the next 3 (rollout head-start; ``history_maintain`` keeps them
  coming). No DEFAULT partition on purpose. Each parent also gets a BRIN index on ``ts``
  (``<table>_ts_brin``); on a partitioned parent ``CREATE INDEX`` cascades to every
  current and future partition. It serves the time-only scans (compaction, retention
  checks) that the ``(station_id, metric_key, ts)`` PK cannot; append-mostly ``ts`` makes
  BRIN tiny. Not in the model state — SQLite has no such index.

The RunPython runs *after* the state operations so its ``apps`` already knows the models
(inside ``SeparateDatabaseAndState.database_operations`` it would only see the old state).
Only ``apps.history.partitions`` (pure helpers) is imported — never the live models.
"""

from datetime import UTC, datetime

import django.db.models.deletion
from django.db import migrations, models

from apps.history import partitions

INITIAL_PARTITION_MONTHS = 4  # current UTC month + next 3

# Column types mirror what Django generates for the model fields on PostgreSQL
# (BigAutoField station pk → bigint, CharField(64) → varchar(64), FloatField → double
# precision, PositiveIntegerField → integer + CHECK >= 0, DateTimeField → timestamptz).
# The FK is DEFERRABLE INITIALLY DEFERRED like every Django-created FK.
_SERIES_DDL = """
CREATE TABLE {table} (
    "station_id" bigint NOT NULL
        REFERENCES "stations_station" ("id") DEFERRABLE INITIALLY DEFERRED,
    "metric_key" varchar(64) NOT NULL,
    "ts" timestamp with time zone NOT NULL,
    "min" double precision NOT NULL,
    "max" double precision NOT NULL,
    "avg" double precision NOT NULL,{extra}
    PRIMARY KEY ("station_id", "metric_key", "ts")
) PARTITION BY RANGE ("ts")
"""
_COUNT_COLUMN = '\n    "count" integer NOT NULL CHECK ("count" >= 0),'


def create_tables(apps, schema_editor):
    connection = schema_editor.connection
    schema_editor.create_model(apps.get_model("history", "HistoryState"))
    if not partitions.is_partitioned(connection):
        schema_editor.create_model(apps.get_model("history", "MetricSample"))
        schema_editor.create_model(apps.get_model("history", "MetricHourly"))
        return
    first = partitions.month_start(datetime.now(UTC))
    for table, extra in (
        ("history_metricsample", ""),
        ("history_metrichourly", _COUNT_COLUMN),
    ):
        schema_editor.execute(
            _SERIES_DDL.format(table=schema_editor.quote_name(table), extra=extra)
        )
        schema_editor.execute(
            f"CREATE INDEX {schema_editor.quote_name(table + '_ts_brin')} "
            f'ON {schema_editor.quote_name(table)} USING brin ("ts")'
        )
        partitions.ensure_partitions(connection, table, first, INITIAL_PARTITION_MONTHS)


def drop_tables(apps, schema_editor):
    if partitions.is_partitioned(schema_editor.connection):
        # Dropping the partitioned parent drops all its partitions.
        for table in partitions.PARTITIONED_TABLES:
            schema_editor.execute(f"DROP TABLE IF EXISTS {schema_editor.quote_name(table)}")
    else:
        schema_editor.delete_model(apps.get_model("history", "MetricHourly"))
        schema_editor.delete_model(apps.get_model("history", "MetricSample"))
    schema_editor.delete_model(apps.get_model("history", "HistoryState"))



class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('stations', '0031_stationtelemetry_metrics'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='HistoryState',
                    fields=[
                        ('id', models.PositiveSmallIntegerField(default=1, editable=False, primary_key=True, serialize=False)),
                        ('compacted_until', models.DateTimeField(blank=True, null=True, verbose_name='compacted until')),
                    ],
                    options={
                        'verbose_name': 'history state',
                        'verbose_name_plural': 'history state',
                    },
                ),
                migrations.CreateModel(
                    name='MetricHourly',
                    fields=[
                        ('pk', models.CompositePrimaryKey('station', 'metric_key', 'ts', blank=True, editable=False, primary_key=True, serialize=False)),
                        ('metric_key', models.CharField(max_length=64, verbose_name='metric key')),
                        ('ts', models.DateTimeField(verbose_name='timestamp')),
                        ('min', models.FloatField(verbose_name='minimum')),
                        ('max', models.FloatField(verbose_name='maximum')),
                        ('avg', models.FloatField(verbose_name='average')),
                        ('count', models.PositiveIntegerField(verbose_name='sample count')),
                        ('station', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='stations.station', verbose_name='station')),
                    ],
                    options={
                        'verbose_name': 'hourly metric aggregate',
                        'verbose_name_plural': 'hourly metric aggregates',
                        'db_table': 'history_metrichourly',
                    },
                ),
                migrations.CreateModel(
                    name='MetricSample',
                    fields=[
                        ('pk', models.CompositePrimaryKey('station', 'metric_key', 'ts', blank=True, editable=False, primary_key=True, serialize=False)),
                        ('metric_key', models.CharField(max_length=64, verbose_name='metric key')),
                        ('ts', models.DateTimeField(verbose_name='timestamp')),
                        ('min', models.FloatField(verbose_name='minimum')),
                        ('max', models.FloatField(verbose_name='maximum')),
                        ('avg', models.FloatField(verbose_name='average')),
                        ('station', models.ForeignKey(db_index=False, on_delete=django.db.models.deletion.CASCADE, related_name='+', to='stations.station', verbose_name='station')),
                    ],
                    options={
                        'verbose_name': 'metric sample',
                        'verbose_name_plural': 'metric samples',
                        'db_table': 'history_metricsample',
                    },
                ),
            ],
            database_operations=[],
        ),
        migrations.RunPython(create_tables, drop_tables),
    ]
