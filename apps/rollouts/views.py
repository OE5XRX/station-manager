import logging

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.permissions import AreaPermissionMixin
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.supersession import (
    ActiveDeploymentConflictError,
    supersede_pending_for_station,
)
from apps.rollouts import services as rollout_services
from apps.rollouts.grouping import group_stations_by_sequence, ordered_groups
from apps.rollouts.targeting import (
    UpgradeTarget,
    latest_index,
    reason_message,
    resolve_upgrade,
)
from apps.stations.models import RolloutGroup, Station, StationAuditLog

from .forms import SequenceAddForm
from .models import RolloutSequence, RolloutSequenceEntry, current_sequence

logger = logging.getLogger(__name__)


def _best_effort_audit_log(*, station, event_type, message, user=None):
    """Audit logging must never break the real operation.

    Safe to call OUTSIDE a transaction.atomic() block. Inside one,
    use _defer_audit_log so a transient DB error on the audit table
    can't poison the enclosing transaction.
    """
    try:
        StationAuditLog.log(
            station=station,
            event_type=event_type,
            message=message,
            user=user,
        )
    except Exception as exc:
        logger.warning("Audit log write failed (%s): %s", event_type, exc)


def _defer_audit_log(*, station, event_type, message, user=None):
    """Queue a best-effort audit log to run after the current transaction commits.

    Inside a transaction.atomic() block, catching a DatabaseError from an
    INSERT puts the whole transaction into a rollback-only state, which
    then poisons every subsequent query in that block. Queuing the write
    with transaction.on_commit() sidesteps that entirely: the callback
    runs only after the enclosing atomic block has already committed, so
    any failure during the audit INSERT can't mark that block rollback-
    only or abort the real operation.
    """
    station_pk = station.pk
    # Resolve SimpleLazyObject (request.user) to a concrete User *now*,
    # so each on_commit callback doesn't do a separate SELECT for the
    # actor — in batch flows like UpgradeGroupView this was an extra
    # query per station. Touching .pk materializes the lazy wrapper;
    # ._wrapped is then the real User instance (Django's FK descriptor
    # previously stumbled on SimpleLazyObject on some code paths).
    resolved_user = None
    if user is not None and getattr(user, "pk", None):
        resolved_user = getattr(user, "_wrapped", user) or user

    def _write() -> None:
        try:
            # Pass station_id directly — StationAuditLog.log supports
            # it natively, which saves a query AND avoids a ValueError
            # if the station row was deleted between post and commit.
            StationAuditLog.log(
                station_id=station_pk,
                event_type=event_type,
                message=message,
                user=resolved_user,
            )
        except Exception as exc:
            logger.warning("Deferred audit log failed (%s): %s", event_type, exc)

    transaction.on_commit(_write)


class UpgradeStationView(AreaPermissionMixin, View):
    """Create a Deployment targeting exactly this one station."""

    area = "deployments"
    verb = "manage"

    def post(self, request, station_pk):
        station = get_object_or_404(
            Station.objects.select_related("current_image_release", "rollout_group"),
            pk=station_pk,
        )
        upgrade = resolve_upgrade(station)
        if not upgrade.ok:
            # "Nothing newer" is information, everything else needs action
            # (provision the box, import / re-import an image).
            notify = (
                messages.info
                if upgrade.reason in (UpgradeTarget.UP_TO_DATE, UpgradeTarget.NOT_NEWER)
                else messages.error
            )
            notify(request, reason_message(upgrade, station))
            return redirect("stations:station_detail", pk=station.pk)
        target = upgrade.target

        try:
            with transaction.atomic():
                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.STATION,
                    target_station=station,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=request.user,
                )
                DeploymentResult.objects.create(
                    deployment=dep,
                    station=station,
                    status=DeploymentResult.Status.PENDING,
                    previous_version=station.current_os_version or "",
                )
                supersede_pending_for_station(station=station, new_deployment=dep)
        except ActiveDeploymentConflictError as exc:
            messages.error(request, str(exc))
            return redirect("stations:station_detail", pk=station.pk)

        _best_effort_audit_log(
            station=station,
            event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
            message=(
                f"Upgrade triggered: {station.current_os_version or '?'} "
                f"\u2192 {target.tag} (deployment #{dep.pk}) by {request.user.username}"
            ),
            user=request.user,
        )
        messages.success(request, _("Upgrade to %(tag)s queued.") % {"tag": target.tag})
        return redirect("stations:station_detail", pk=station.pk)


class UpgradeGroupView(AreaPermissionMixin, View):
    """Create Deployments for every station in the given rollout group
    (one Deployment per machine, each targeting the newest OTA-ready image of
    the group's channel). Works for any group, in the sequence or not.
    """

    area = "rollouts"
    verb = "manage"

    def post(self, request, group_slug):
        group = get_object_or_404(RolloutGroup, slug=group_slug)
        stations = list(
            Station.objects.filter(rollout_group=group).select_related(
                "current_image_release", "rollout_group"
            )
        )
        if not stations:
            messages.info(request, _("No stations are currently assigned to this group."))
            return redirect("rollouts:upgrade_dashboard")

        # Resolve every station against one prebuilt index. Stations without
        # a platform, without a newer / OTA-ready image, or already up to date
        # are counted as skipped so the flash message doesn't silently lose
        # them. The rest are bucketed by machine (all stations of a group
        # share its channel, so one machine means one target image).
        index = latest_index()
        by_machine: dict[str, tuple] = {}
        skipped = 0
        for s in stations:
            upgrade = resolve_upgrade(s, index)
            if not upgrade.ok:
                skipped += 1
                continue
            target = upgrade.target
            by_machine.setdefault(target.machine, (target, []))[1].append(s)

        created = 0
        with transaction.atomic():
            for target, eligible in by_machine.values():
                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.GROUP,
                    target_group=group,
                    status=Deployment.Status.IN_PROGRESS,
                    created_by=request.user,
                )
                for s in eligible:
                    DeploymentResult.objects.create(
                        deployment=dep,
                        station=s,
                        status=DeploymentResult.Status.PENDING,
                        previous_version=s.current_os_version or "",
                    )
                    try:
                        supersede_pending_for_station(station=s, new_deployment=dep)
                    except ActiveDeploymentConflictError:
                        # Drop this station from the deployment - it will
                        # be picked up next time.
                        DeploymentResult.objects.filter(deployment=dep, station=s).delete()
                        skipped += 1
                        continue
                    # Defer audit-log write until AFTER the atomic block
                    # commits. Running it inside the block risks a
                    # DatabaseError on the audit table flipping the main
                    # transaction into rollback-only, which would then
                    # swallow every subsequent create in the loop.
                    _defer_audit_log(
                        station=s,
                        event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                        message=(
                            f"Upgrade triggered (group '{group.name}', channel {group.channel}): "
                            f"{s.current_os_version or '?'} \u2192 {target.tag} "
                            f"(deployment #{dep.pk}) by {request.user.username}"
                        ),
                        user=request.user,
                    )
                    created += 1

                # If every station in this wave conflicted, back out the
                # empty deployment we just created.
                if not dep.results.exists():
                    dep.delete()

        messages.success(
            request,
            _("Queued %(n)d upgrades (%(s)d skipped)") % {"n": created, "s": skipped},
        )
        return redirect("rollouts:upgrade_dashboard")


class UpgradeDashboardView(AreaPermissionMixin, TemplateView):
    """Roll-up of every station bucketed by its rollout group, showing the
    channel-aware upgrade target per station and a per-group action.
    """

    area = "rollouts"

    template_name = "rollouts/upgrade_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        stations = list(Station.objects.select_related("current_image_release", "rollout_group"))
        groups = ordered_groups()
        grouped = group_stations_by_sequence(stations, groups)
        group_by_slug = {g.slug: g for g in groups}
        index = latest_index()

        # Fetch the latest active DeploymentResult per station so the
        # initial page render already shows real deployment progress
        # (pending/downloading/installing/rebooting/verifying) instead
        # of waiting for the first WebSocket event to correct the UI.
        active_statuses = [
            DeploymentResult.Status.PENDING,
            DeploymentResult.Status.DOWNLOADING,
            DeploymentResult.Status.INSTALLING,
            DeploymentResult.Status.REBOOTING,
            DeploymentResult.Status.VERIFYING,
        ]
        # stations already holds the whole fleet (the dashboard renders
        # every station), so an IN-clause over [s.pk for s in stations]
        # would be a no-op that just risks PG's parameter limit as the
        # fleet grows. Fetch every active result; the setdefault below
        # keeps the latest per station_id.
        active_result_by_station: dict[int, DeploymentResult] = {}
        for r in (
            DeploymentResult.objects.filter(
                status__in=active_statuses,
                deployment__status=Deployment.Status.IN_PROGRESS,
            )
            .order_by("station_id", "-pk")
            .select_related("deployment__image_release")
        ):
            active_result_by_station.setdefault(r.station_id, r)

        group_rows: list[dict] = []
        up_to_date: list = []
        for group_key, stations_in_group in grouped.items():
            rows = []
            for s in stations_in_group:
                upgrade = resolve_upgrade(s, index)
                if upgrade.reason == UpgradeTarget.UP_TO_DATE:
                    up_to_date.append((s, upgrade.target))
                    continue
                rows.append(
                    {
                        "station": s,
                        "upgrade": upgrade,
                        "message": reason_message(upgrade, s),
                        "active_result": active_result_by_station.get(s.pk),
                    }
                )
            group = group_by_slug.get(group_key)
            group_rows.append(
                {
                    "key": group_key,
                    "name": group.name if group else _("No group"),
                    "channel": group.channel if group else RolloutGroup.Channel.RELEASE,
                    "is_unassigned": group is None,
                    "is_empty": not stations_in_group,
                    "rows": rows,
                    "upgradable": sum(1 for row in rows if row["upgrade"].ok),
                }
            )

        ctx["groups"] = group_rows
        ctx["up_to_date"] = up_to_date
        ctx["latest_releases"] = sorted(index.values(), key=lambda r: (r.machine, r.channel))
        return ctx


class SequenceEditView(AreaPermissionMixin, TemplateView):
    area = "rollouts"
    verb = "manage"
    template_name = "rollouts/sequence_edit.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        seq = current_sequence()
        ctx["sequence"] = seq
        ctx["entries"] = seq.entries.select_related("group").order_by("position")
        ctx["add_form"] = SequenceAddForm(sequence=seq)
        return ctx


class SequenceAddView(AreaPermissionMixin, View):
    area = "rollouts"
    verb = "manage"

    def post(self, request):
        seq = current_sequence()
        form = SequenceAddForm(request.POST, sequence=seq)
        if form.is_valid():
            # The service locks the sequence row, re-checks uniqueness under
            # the lock (idempotent no-op if a concurrent click won) and
            # appends at max(position) + 1. It used to be duplicated here as
            # `(max or -1) + 1`, which turned max == 0 into position 0 again
            # and 500'd on the second entry (uniq_position_per_sequence).
            rollout_services.add_entry(seq, form.cleaned_data["group"], by_user=request.user)
        return redirect("rollouts:sequence_edit")


class SequenceRemoveView(AreaPermissionMixin, View):
    area = "rollouts"
    verb = "manage"

    def post(self, request, entry_pk):
        seq = current_sequence()
        entry = get_object_or_404(RolloutSequenceEntry, pk=entry_pk, sequence=seq)
        # Locked delete + gap-free renumber + updated_by stamp, all atomic.
        rollout_services.remove_entry(entry=entry, by_user=request.user)
        return redirect("rollouts:sequence_edit")


class SequenceReorderView(AreaPermissionMixin, View):
    area = "rollouts"
    verb = "manage"

    def post(self, request):
        seq = current_sequence()
        order_str = request.POST.get("order", "")
        if not order_str:
            return HttpResponseBadRequest("order required")
        try:
            order_ids = [int(x) for x in order_str.split(",") if x]
        except ValueError:
            return HttpResponseBadRequest("order must be ids")
        # A payload like "1,1,2" would pass the later set() comparison
        # (because set({1,1,2}) == {1,2}) but then assign two positions
        # to entry 1 and skip one — leaving the sequence non-normalized.
        if len(order_ids) != len(set(order_ids)):
            return HttpResponseBadRequest("order must not contain duplicates")
        with transaction.atomic():
            # Serialize against concurrent Add/Remove/Reorder on the
            # same sequence through a row lock on the RolloutSequence
            # itself — matches SequenceAddView's behaviour.
            RolloutSequence.objects.select_for_update().filter(pk=seq.pk).first()
            # Read entries *after* acquiring the sequence lock. Reading
            # beforehand races with a concurrent Remove: if a row is
            # deleted between our read and the lock, the later e.save()
            # finds no row to UPDATE and Django falls back to INSERT,
            # re-inserting a deleted entry.
            existing = {e.pk: e for e in seq.entries.select_for_update()}
            if set(order_ids) != set(existing.keys()):
                return HttpResponseBadRequest("order must match existing entries")
            # Two-phase update so the per-sequence position unique
            # constraint never sees a collision during the transition.
            # Pick an offset that can't overlap with either the old or
            # the new positions, clamped to PositiveSmallIntegerField
            # (max 32767). The first pass writes e.position + offset,
            # so the real ceiling is max_current + offset — not
            # offset + n.
            n = len(order_ids)
            max_current = max((e.position for e in existing.values()), default=0)
            offset = max(max_current, n) + 1
            if max_current + offset > 32767:
                return HttpResponseBadRequest("sequence too large to reorder safely")
            for e in existing.values():
                e.position = e.position + offset
                e.save(update_fields=["position"])
            for idx, pk in enumerate(order_ids):
                e = existing[pk]
                e.position = idx
                e.save(update_fields=["position"])
            # Keep the updated_by/updated_at bump inside the same
            # atomic block as the reorder writes — matches the
            # Add/Remove semantics, and a failure here won't commit a
            # reorder without the metadata or vice versa.
            seq.updated_by = request.user
            seq.save(update_fields=["updated_by", "updated_at"])
        return HttpResponse(status=200)
