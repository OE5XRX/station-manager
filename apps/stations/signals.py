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
    # Bestehender StationAuditLog-Eintrag (unverändert):
    StationAuditLog.log(
        station=instance.station,
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        user=instance.assigned_by,
        message=f"{instance.user} → {instance.get_role_display()}",
    )
    # NEU in 1a: zusätzlich AccountAuditLog mit target_user=<assignee>
    # so dass User-Detail-Audit-Tab das findet (Subjekt = User).
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_CREATED,
        actor=instance.assigned_by,
        target_user=instance.user,
        message=(
            f"station={instance.station.callsign or instance.station.name}, "
            f"role={instance.get_role_display()}"
        ),
    )


@receiver(post_delete, sender=StationAssignment)
def _on_station_assignment_delete(sender, instance, **kwargs):
    # Bestehender StationAuditLog-Eintrag (unverändert):
    StationAuditLog.log(
        station=instance.station,
        event_type=StationAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        user=None,
        message=(f"{instance.user} ({instance.get_role_display()}) entfernt"),
    )
    # NEU in 1a: zusätzlich AccountAuditLog.
    # Sub-Spec 2b §4: callers (e.g. UserSoftDeleteView) can stash a
    # ``_revoke_reason`` on the instance before calling .delete() so the
    # forensic message captures WHY the assignment went away. Without
    # the marker, the legacy message format is kept verbatim.
    label = instance.station.callsign or instance.station.name
    reason = getattr(instance, "_revoke_reason", None)
    role_display = instance.get_role_display()
    if reason:
        message = f"reason={reason} station={label} role={role_display}"
    else:
        message = f"station={label}, role={role_display}"
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.STATION_ASSIGNMENT_REVOKED,
        actor=getattr(instance, "_revoke_actor", None),
        target_user=instance.user,
        message=message,
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

    # Hot path: heartbeat updates pass update_fields=["last_seen", ...]
    # which never touches region. Skip the DB read entirely in that
    # case. (None means "all fields" — fall through and check.)
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "region" not in update_fields:
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
    # Sub-Spec 2b §4: callers can stash ``_revoke_reason`` + ``_revoke_actor``
    # on the instance before .delete() so soft-delete-driven revokes carry
    # their reason in the audit message and the originating admin as actor.
    reason = getattr(instance, "_revoke_reason", None)
    role_display = instance.get_role_display()
    if reason:
        message = f"reason={reason} region={instance.region.name} role={role_display}"
    else:
        message = f"role={role_display} entfernt"
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REGION_ASSIGNMENT_REVOKED,
        actor=getattr(instance, "_revoke_actor", None),
        target_user=instance.user,
        region=instance.region,
        message=message,
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
