import logging

from django.contrib import messages
from django.db import transaction
from django.db.models import Max
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from apps.accounts.views import AdminRequiredMixin
from apps.deployments.models import Deployment, DeploymentResult
from apps.deployments.supersession import (
    ActiveDeploymentConflictError,
    supersede_pending_for_station,
)
from apps.images.models import ImageRelease
from apps.rollouts.grouping import UNASSIGNED_KEY, group_stations_by_sequence
from apps.stations.models import Station, StationAuditLog, StationTag

from .forms import SequenceAddForm
from .models import RolloutSequenceEntry, current_sequence

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
    with transaction.on_commit() sidesteps that entirely: the call fires
    only if the outer transaction actually commits, and any failure then
    is on its own connection.
    """
    station_pk = station.pk
    user_pk = getattr(user, "pk", None)

    def _write() -> None:
        try:
            # Re-resolve the Station/User by pk so we don't hold stale
            # instances from the transaction across the commit boundary.
            actor = type(user).objects.filter(pk=user_pk).first() if user_pk else None
            StationAuditLog.log(
                station=Station.objects.filter(pk=station_pk).first(),
                event_type=event_type,
                message=message,
                user=actor,
            )
        except Exception as exc:
            logger.warning("Deferred audit log failed (%s): %s", event_type, exc)

    transaction.on_commit(_write)


def _target_release_for(station) -> ImageRelease | None:
    """Look up the latest ImageRelease for the station's machine.

    Machine is taken from station.current_image_release.machine; if the
    station has never been provisioned through our flow, return None.
    """
    current = getattr(station, "current_image_release", None)
    if current is None:
        return None
    return ImageRelease.objects.filter(machine=current.machine, is_latest=True).first()


class UpgradeStationView(AdminRequiredMixin, View):
    """Create a Deployment targeting exactly this one station."""

    def post(self, request, station_pk):
        station = get_object_or_404(Station, pk=station_pk)
        target = _target_release_for(station)
        if target is None:
            messages.error(
                request,
                _("No image release available for this station's machine."),
            )
            return redirect("stations:station_detail", pk=station.pk)

        if station.current_image_release_id == target.pk:
            messages.info(request, _("Station is already on the latest release."))
            return redirect("stations:station_detail", pk=station.pk)

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


class UpgradeGroupView(AdminRequiredMixin, View):
    """Create Deployments for every station carrying the given tag (grouped
    by machine: one Deployment per (tag, machine) tuple).
    """

    def post(self, request, tag_slug):
        tag = get_object_or_404(StationTag, slug=tag_slug)

        # Honor first-match-wins: a station carrying both "test" (pos 0)
        # and "easy" (pos 1) belongs to whichever tag comes first in the
        # sequence. Resolve the same bucketing as the dashboard, but
        # purely in SQL — group-upgrade should scale with group size,
        # not total fleet size.
        seq = current_sequence()
        entry = seq.entries.filter(tag=tag).first()
        if entry is None:
            messages.info(request, _("Tag is not part of the rollout sequence."))
            return redirect("rollouts:upgrade_dashboard")

        earlier_tag_ids = list(
            seq.entries.filter(position__lt=entry.position).values_list("tag_id", flat=True)
        )
        stations_qs = Station.objects.filter(tags=tag).select_related("current_image_release")
        if earlier_tag_ids:
            # Narrow the claimed-by-earlier query to stations that *also*
            # carry the target tag — chained .filter() on an M2M forces
            # an AND intersection. Bound the query to the group, not the
            # fleet. Pass the queryset to .exclude() so Django emits a
            # subquery instead of materializing pks into Python.
            claimed_by_earlier = (
                Station.objects.filter(tags=tag).filter(tags__in=earlier_tag_ids).values("pk")
            )
            stations_qs = stations_qs.exclude(pk__in=claimed_by_earlier)
        stations = list(stations_qs)
        if not stations:
            messages.info(request, _("No stations are currently assigned to this group."))
            return redirect("rollouts:upgrade_dashboard")

        # Bucket by machine. Stations with no current_image_release cannot
        # be routed to a target (we don't know their machine) — count them
        # as skipped so the flash message doesn't silently lose them.
        by_machine: dict[str, list] = {}
        unprovisioned = 0
        for s in stations:
            if not s.current_image_release:
                unprovisioned += 1
                continue
            by_machine.setdefault(s.current_image_release.machine, []).append(s)

        created = 0
        skipped = unprovisioned
        with transaction.atomic():
            for machine, machine_stations in by_machine.items():
                target = ImageRelease.objects.filter(machine=machine, is_latest=True).first()
                if target is None:
                    skipped += len(machine_stations)
                    continue

                # Filter out stations that are already on the target or
                # that have an active deployment — BEFORE creating the
                # Deployment row, so an all-skipped machine doesn't leave
                # an empty Deployment floating in the table.
                eligible: list = []
                for s in machine_stations:
                    if s.current_image_release_id == target.pk:
                        skipped += 1
                        continue
                    eligible.append(s)

                if not eligible:
                    continue

                dep = Deployment.objects.create(
                    image_release=target,
                    target_type=Deployment.TargetType.TAG,
                    target_tag=tag,
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
                    # swallow every subsequent create in the loop. The
                    # lambda copies all values we need so the closure is
                    # safe even though `s` / `dep` / `target` / `tag` are
                    # loop variables that will change.
                    _defer_audit_log(
                        station=s,
                        event_type=StationAuditLog.EventType.FIRMWARE_UPDATE,
                        message=(
                            f"Upgrade triggered (group '{tag.name}'): "
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


class UpgradeDashboardView(AdminRequiredMixin, TemplateView):
    """Admin-only roll-up of every station bucketed by its first matching
    rollout-sequence tag, showing pending upgrades and a per-group action.
    """

    template_name = "rollouts/upgrade_dashboard.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        stations = list(
            Station.objects.select_related("current_image_release").prefetch_related("tags")
        )
        grouped = group_stations_by_sequence(stations)

        latest_per_machine: dict[str, ImageRelease] = {
            r.machine: r for r in ImageRelease.objects.filter(is_latest=True)
        }

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

        # grouped keys are tag slugs; the sidebar + headers want the human
        # name. One prefetch covers every tag that actually matters here.
        slug_to_name = dict(StationTag.objects.values_list("slug", "name"))

        rows_by_group: list[tuple[str, str, list]] = []
        up_to_date: list = []
        for group_key, stations_in_group in grouped.items():
            pending = []
            for s in stations_in_group:
                target = (
                    latest_per_machine.get(s.current_image_release.machine)
                    if s.current_image_release
                    else None
                )
                if target and s.current_image_release_id == target.pk:
                    up_to_date.append((s, target))
                else:
                    pending.append((s, target, active_result_by_station.get(s.pk)))
            if group_key == UNASSIGNED_KEY:
                display_name = _("Unassigned")
            else:
                display_name = slug_to_name.get(group_key, group_key)
            rows_by_group.append((group_key, display_name, pending))

        ctx["groups"] = rows_by_group
        ctx["up_to_date"] = up_to_date
        ctx["latest_per_machine"] = latest_per_machine
        ctx["unassigned_key"] = UNASSIGNED_KEY
        return ctx


class SequenceEditView(AdminRequiredMixin, TemplateView):
    template_name = "rollouts/sequence_edit.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        seq = current_sequence()
        ctx["sequence"] = seq
        ctx["entries"] = seq.entries.select_related("tag").order_by("position")
        ctx["add_form"] = SequenceAddForm(sequence=seq)
        return ctx


class SequenceAddView(AdminRequiredMixin, View):
    def post(self, request):
        seq = current_sequence()
        form = SequenceAddForm(request.POST, sequence=seq)
        if form.is_valid():
            next_pos = (seq.entries.aggregate(Max("position"))["position__max"] or -1) + 1
            RolloutSequenceEntry.objects.create(
                sequence=seq,
                tag=form.cleaned_data["tag"],
                position=next_pos,
            )
            seq.updated_by = request.user
            seq.save(update_fields=["updated_by", "updated_at"])
        return redirect("rollouts:sequence_edit")


class SequenceRemoveView(AdminRequiredMixin, View):
    def post(self, request, entry_pk):
        seq = current_sequence()
        entry = get_object_or_404(RolloutSequenceEntry, pk=entry_pk, sequence=seq)
        # Delete + normalize positions + bump sequence metadata all in
        # one atomic block so a failure mid-way doesn't leave the
        # sequence with a gap or with updated_by unset.
        with transaction.atomic():
            entry.delete()
            for idx, e in enumerate(seq.entries.order_by("position")):
                if e.position != idx:
                    e.position = idx
                    e.save(update_fields=["position"])
            seq.updated_by = request.user
            seq.save(update_fields=["updated_by", "updated_at"])
        return redirect("rollouts:sequence_edit")


class SequenceReorderView(AdminRequiredMixin, View):
    def post(self, request):
        seq = current_sequence()
        order_str = request.POST.get("order", "")
        if not order_str:
            return HttpResponseBadRequest("order required")
        try:
            order_ids = [int(x) for x in order_str.split(",") if x]
        except ValueError:
            return HttpResponseBadRequest("order must be ids")
        existing = {e.pk: e for e in seq.entries.all()}
        if set(order_ids) != set(existing.keys()):
            return HttpResponseBadRequest("order must match existing entries")
        with transaction.atomic():
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
        seq.updated_by = request.user
        seq.save(update_fields=["updated_by", "updated_at"])
        return HttpResponse(status=200)
