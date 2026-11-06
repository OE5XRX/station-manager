from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from .models import Repeater
from .oevsv import OevsvError

FIELDS = (
    "callsign",
    "site_name",
    "locator",
    "output_frequency",
    "input_frequency",
    "input_tone",
    "output_tone",
    "latitude",
    "longitude",
    "active",
)


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped: int = 0


@transaction.atomic
def sync_repeaters(rows: list[dict], now: datetime) -> SyncResult:
    """Upsert ÖVSV rows by ``external_id``; never touches manual entries."""
    result = SyncResult()
    existing = {r.external_id: r for r in Repeater.objects.filter(source=Repeater.Source.OEVSV)}
    active_before = sum(1 for r in existing.values() if r.active)
    if active_before and (not rows or (active_before >= 10 and len(rows) < active_before / 2)):
        raise OevsvError(f"refusing sync: {len(rows)} rows vs {active_before} active")
    seen = set()
    for row in rows:
        ext = row.get("external_id")
        if not ext or ext in seen:
            result.skipped += 1
            continue
        seen.add(ext)
        obj = existing.get(ext)
        if obj is None:
            Repeater.objects.create(
                source=Repeater.Source.OEVSV,
                external_id=ext,
                last_synced_at=now,
                **{f: row[f] for f in FIELDS},
            )
            result.created += 1
            continue
        changed = [f for f in FIELDS if getattr(obj, f) != row[f]]
        if obj.active and not row["active"]:
            result.deactivated += 1
        for f in changed:
            setattr(obj, f, row[f])
        obj.last_synced_at = now
        obj.save(update_fields=[*changed, "last_synced_at", "updated_at"])
        if changed:
            result.updated += 1
    gone = Repeater.objects.filter(source=Repeater.Source.OEVSV, active=True).exclude(
        external_id__in=seen
    )
    result.deactivated += gone.update(active=False)
    return result
