"""Monthly RANGE-partition helpers for the history tables.

Pure helpers (month math, names) work everywhere; the DDL helpers only act on PostgreSQL
and are no-ops (``[]``/``None``) on other backends, where the tables are not partitioned.

Imported by ``migrations/0001_initial.py`` — keep this module free of model imports.
All boundaries are UTC month starts; partitions are named ``<table>_pYYYYMM`` and their
bounds are derived from that name (``list_partitions`` ignores anything else).
"""

import re
from datetime import UTC, datetime

PARTITIONED_TABLES = ("history_metricsample", "history_metrichourly")

_NAME_RE = re.compile(r"^(?P<table>.+)_p(?P<year>\d{4})(?P<month>\d{2})$")


def month_start(dt: datetime) -> datetime:
    """UTC start (day 1, 00:00) of the month containing ``dt`` (naive = UTC)."""
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(dt: datetime, n: int) -> datetime:
    """``month_start(dt)`` shifted by ``n`` months (may be negative)."""
    base = month_start(dt)
    idx = base.year * 12 + (base.month - 1) + n
    return base.replace(year=idx // 12, month=idx % 12 + 1)


def partition_name(table: str, month: datetime) -> str:
    m = month_start(month)
    return f"{table}_p{m.year:04d}{m.month:02d}"


def is_partitioned(connection) -> bool:
    return connection.vendor == "postgresql"


def _bound(dt: datetime) -> str:
    # Built only from a datetime we computed (never user input); DDL cannot take params.
    return "'" + dt.strftime("%Y-%m-%d %H:%M:%S+00") + "'"


def ensure_partitions(connection, table: str, first_month: datetime, count: int) -> list[str]:
    """Create the monthly partitions ``first_month … +count-1`` that don't exist yet.

    Returns the names actually created (idempotent; ``[]`` on non-Postgres).
    """
    if not is_partitioned(connection) or count <= 0:
        return []
    if table not in PARTITIONED_TABLES:
        raise ValueError(f"not a history table: {table!r}")
    existing = {name for name, _lo, _hi in list_partitions(connection, table)}
    qn = connection.ops.quote_name
    created = []
    with connection.cursor() as cur:
        for i in range(count):
            lo = add_months(first_month, i)
            name = partition_name(table, lo)
            if name in existing:
                continue
            hi = add_months(lo, 1)
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {qn(name)} PARTITION OF {qn(table)} "
                f"FOR VALUES FROM ({_bound(lo)}) TO ({_bound(hi)})"
            )
            created.append(name)
    return created


def list_partitions(connection, table: str) -> list[tuple[str, datetime, datetime]]:
    """``(name, lo, hi)`` of ``table``'s ``<table>_pYYYYMM`` partitions, sorted by ``lo``."""
    if not is_partitioned(connection):
        return []
    with connection.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_inherits i "
            "JOIN pg_class c ON c.oid = i.inhrelid "
            "JOIN pg_class p ON p.oid = i.inhparent "
            "JOIN pg_namespace n ON n.oid = p.relnamespace "
            "WHERE p.relname = %s AND n.nspname = current_schema()",
            [table],
        )
        names = [row[0] for row in cur.fetchall()]
    result = []
    for name in names:
        m = _NAME_RE.match(name)
        if not m or m["table"] != table or not 1 <= int(m["month"]) <= 12:
            continue
        lo = datetime(int(m["year"]), int(m["month"]), 1, tzinfo=UTC)
        result.append((name, lo, add_months(lo, 1)))
    result.sort(key=lambda r: r[1])
    return result


def drop_partition(connection, name: str) -> None:
    """``DROP TABLE IF EXISTS`` one partition (no-op on non-Postgres)."""
    if not is_partitioned(connection):
        return
    m = _NAME_RE.match(name)
    if not m or m["table"] not in PARTITIONED_TABLES:
        raise ValueError(f"not a history partition: {name!r}")
    with connection.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {connection.ops.quote_name(name)}")
