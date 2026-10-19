"""Firmware convergence overview + quarantine release (GUI)."""

from django.contrib import messages
from django.db.models import Count, Prefetch
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views import View
from django.views.generic import ListView

from apps.accounts.client_ip import client_ip
from apps.accounts.permissions import AreaPermissionMixin
from apps.stations.models import Station

from . import ops, services
from .models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareRelease,
    ModuleType,
)
from .reconciler import effective_target


def is_int_param(value):
    """ASCII-digits-only query param guard. ``str.isdigit()`` also accepts
    Unicode digits like '²', which ``int()`` then rejects (500)."""
    return value.isascii() and value.isdecimal()


def firmware_row(module, assignment, *, release_keys=None):
    """Firmware facts for one module at its (open) ``assignment``.

    ``target``: effective ModuleFirmwareTarget (None without an assigned
    station or when nothing applies). ``has_release``: an active release for
    the module's variant at the target version exists. ``quarantine``: only
    while the module's rollup is QUARANTINED — the QUARANTINED convergence
    state for the current target release, falling back to the latest; else
    None (``services.current_quarantine``). Uses a ``quarantined_states``
    prefetch (to_attr) on ``module`` when present, and a precomputed
    ``release_keys`` set of active (module_type_id, variant, version) tuples
    instead of a per-row EXISTS query when given."""
    target = None
    if assignment is not None and assignment.station is not None:
        target = effective_target(assignment.station, module.module_type)
    if target is None:
        has_release = False
    elif release_keys is not None:
        has_release = (module.module_type_id, module.variant, target.version) in release_keys
    else:
        has_release = ModuleFirmwareRelease.objects.filter(
            module_type_id=module.module_type_id,
            variant=module.variant,
            version=target.version,
        ).exists()
    # Same row selection as release_quarantine (services.current_quarantine),
    # so the shown Release button always acts on the shown row.
    quarantine = services.current_quarantine(
        module, states=getattr(module, "quarantined_states", None), target=target
    )
    return {"target": target, "has_release": has_release, "quarantine": quarantine}


class ConvergenceOverviewView(AreaPermissionMixin, ListView):
    area = "modules"
    model = Module
    template_name = "module_firmware/convergence.html"
    context_object_name = "modules"
    paginate_by = 50

    def get_queryset(self):
        open_assignments = ModuleAssignmentHistory.objects.filter(
            to_ts__isnull=True
        ).select_related("station")
        qs = Module.objects.select_related("module_type").prefetch_related(
            Prefetch("assignments", queryset=open_assignments, to_attr="open_assignments"),
            Prefetch(
                "convergence_states",
                queryset=services.quarantined_states_qs(),
                to_attr="quarantined_states",
            ),
        )
        g = self.request.GET
        if g.get("type"):
            qs = qs.filter(module_type__key=g["type"])
        if g.get("state"):
            qs = qs.filter(firmware_convergence=g["state"])
        if is_int_param(g.get("station", "")):
            qs = qs.filter(
                assignments__station_id=g["station"], assignments__to_ts__isnull=True
            ).distinct()
        if g.get("q"):
            qs = qs.filter(uid__icontains=g["q"])
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        modules = list(ctx["modules"])
        # One query for the page's release keys instead of an EXISTS per row.
        release_keys = ops.active_release_keys({m.module_type_id for m in modules})
        rows = []
        for module in modules:
            assignment = module.open_assignments[0] if module.open_assignments else None
            rows.append(
                {
                    "module": module,
                    "assignment": assignment,
                    **firmware_row(module, assignment, release_keys=release_keys),
                }
            )
        ctx["rows"] = rows
        # One aggregate over the whole fleet (not the filtered page).
        counts = dict(Module.objects.values_list("firmware_convergence").annotate(n=Count("id")))
        ctx["kpi_list"] = [
            (value, label, counts.get(value, 0)) for value, label in Module.Convergence.choices
        ]
        ctx["states"] = Module.Convergence.choices
        ctx["types"] = ModuleType.objects.all()
        # pk__in, not a reverse-FK filter: that LEFT JOINs and lists
        # stations without any assignment.
        ctx["stations"] = Station.objects.filter(
            pk__in=ModuleAssignmentHistory.objects.filter(to_ts__isnull=True).values("station_id")
        ).order_by("name")
        g = self.request.GET
        ctx["filters"] = {
            "type": g.get("type", ""),
            "state": g.get("state", ""),
            "station": g.get("station", ""),
            "q": g.get("q", ""),
        }
        return ctx


class ModuleReleaseQuarantineView(AreaPermissionMixin, View):
    area = "modules"
    verb = "manage"

    def post(self, request, uid):
        module = get_object_or_404(Module, uid=uid)
        versions = services.release_quarantine(
            module, user=request.user, ip_address=client_ip(request)
        )
        if versions:
            messages.success(
                request,
                _("Quarantine released for %(uid)s (%(versions)s); the update will be retried.")
                % {"uid": module.uid, "versions": ", ".join(versions)},
            )
        else:
            messages.info(request, _("%(uid)s is not quarantined.") % {"uid": module.uid})
        nxt = request.POST.get("next", "")
        if nxt and url_has_allowed_host_and_scheme(
            nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return HttpResponseRedirect(nxt)
        return HttpResponseRedirect(reverse("module_firmware:module_detail", args=[uid]))
