"""Signal handlers that emit audit-log entries for topology mutations.

Signal-based emission catches every save/delete path: views, Django
Admin, shell, direct ORM. Migration 0005 (Group -> membership_level
seed) does NOT emit because data migrations run before AppConfig.ready
registers these handlers — that's the documented limitation in the
spec.

Membership-level promote/demote audit emission is NOT here: it lives
in the promote/demote view (PR-3) because the view has the actor
context that signals lack.

Station.region change uses a pre_save + post_save pair: pre_save
records the pre-mutation FK so post_save can compute the diff. We
stash the diff on the instance via a private attribute that gets
deleted after emission.
"""

from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from apps.accounts.models import AccountAuditLog
from apps.stations.models import (
    Region,
    RegionAssignment,
    Station,
    StationAssignment,
    StationAuditLog,
)

# --- StationAssignment ---


@receiver(post_save, sender=StationAssignment)
def _on_station_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    StationAuditLog.log(
        station=instance.station,
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        user=instance.assigned_by,
        message=f"{instance.user} → {instance.get_role_display()}",
    )


@receiver(post_delete, sender=StationAssignment)
def _on_station_assignment_delete(sender, instance, **kwargs):
    StationAuditLog.log(
        station=instance.station,
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        user=None,
        message=(f"{instance.user} ({instance.get_role_display()}) entfernt"),
    )


# --- Station.region ---

_PENDING_REGION_ATTR = "_pending_region_change"


@receiver(pre_save, sender=Station)
def _on_station_pre_save(sender, instance, **kwargs):
    # Clear any stale pending-change from a previous (possibly
    # failed) save attempt before re-checking. Makes pre_save
    # authoritative on every call.
    if hasattr(instance, _PENDING_REGION_ATTR):
        delattr(instance, _PENDING_REGION_ATTR)

    if not instance.pk:
        return
    try:
        old = Station.objects.only("region_id").get(pk=instance.pk)
    except Station.DoesNotExist:
        return
    if old.region_id != instance.region_id:
        setattr(
            instance,
            _PENDING_REGION_ATTR,
            (old.region_id, instance.region_id),
        )


@receiver(post_save, sender=Station)
def _on_station_save(sender, instance, created, **kwargs):
    change = getattr(instance, _PENDING_REGION_ATTR, None)
    if not change:
        return
    old_id, new_id = change
    old_name = (
        Region.objects.filter(pk=old_id).values_list("name", flat=True).first() if old_id else None
    )
    new_name = (
        Region.objects.filter(pk=new_id).values_list("name", flat=True).first() if new_id else None
    )
    StationAuditLog.log(
        station=instance,
        event_type=StationAuditLog.EventType.STATION_REGION_CHANGED,
        user=None,
        message=f"{old_name or '∅'} → {new_name or '∅'}",
    )
    delattr(instance, _PENDING_REGION_ATTR)


# --- RegionAssignment ---


@receiver(post_save, sender=RegionAssignment)
def _on_region_assignment_save(sender, instance, created, **kwargs):
    if not created:
        return
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_CREATED,
        actor=instance.assigned_by,
        target_user=instance.user,
        region=instance.region,
        message=f"role={instance.get_role_display()}",
    )


@receiver(post_delete, sender=RegionAssignment)
def _on_region_assignment_delete(sender, instance, **kwargs):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        target_user=instance.user,
        region=instance.region,
        message=f"role={instance.get_role_display()} entfernt",
    )


# --- Region ---


@receiver(post_save, sender=Region)
def _on_region_save(sender, instance, created, **kwargs):
    if created:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_CREATED,
            region=instance,
            message=f"created: {instance.name}",
        )
    else:
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.REGION_UPDATED,
            region=instance,
            message=f"updated: {instance.name}",
        )


@receiver(post_delete, sender=Region)
def _on_region_delete(sender, instance, **kwargs):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_DELETED,
        region=None,  # FK is gone after delete
        message=f"deleted: {instance.name}",
    )
