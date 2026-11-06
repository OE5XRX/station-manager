from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Max
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import CreateView, ListView, TemplateView, UpdateView, View

from apps.accounts.client_ip import client_ip
from apps.accounts.models import AccountAuditLog
from apps.accounts.permissions import AreaPermissionMixin

from .forms import RepeaterForm, ScanListEntryForm, ScanListForm
from .models import Repeater, ScanList, ScanListEntry
from .permissions import can_manage_repeaters

ATTRIBUTION = {
    "text": "Data: ÖVSV repeater database, CC BY 4.0",
    "url": "https://repeater.oevsv.at/",
}


def _audit(request, message, repeater, changes=None):
    AccountAuditLog.log(
        event_type=AccountAuditLog.EventType.REPEATER_CHANGED,
        actor=request.user,
        message=message,
        ip_address=client_ip(request),
        details={"repeater_id": repeater.pk, "changes": changes or {}},
    )


class ManageRequiredMixin:
    """Writes need ``can_manage_repeaters`` (repeaters.manage OR region manager)."""

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not can_manage_repeaters(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class ManualOnlyMixin:
    """ÖVSV rows are owned by the sync; only manual rows are editable."""

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        if obj.source != Repeater.Source.MANUAL:
            raise PermissionDenied
        return obj


class RepeaterListView(AreaPermissionMixin, ListView):
    area = "repeaters"
    model = Repeater
    template_name = "repeaters/repeater_list.html"
    context_object_name = "repeaters"

    def show_all(self):
        return self.request.GET.get("show") == "all"

    def get_queryset(self):
        qs = Repeater.objects.select_related("replaces")
        if not self.show_all():
            qs = qs.visible()
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(
            show_all=self.show_all(),
            can_manage=can_manage_repeaters(self.request.user),
            attribution=ATTRIBUTION,
        )
        return ctx


class RepeaterCreateView(AreaPermissionMixin, ManageRequiredMixin, CreateView):
    area = "repeaters"
    verbs = {"GET": "view", "POST": "view"}
    model = Repeater
    form_class = RepeaterForm
    template_name = "repeaters/repeater_form.html"
    success_url = reverse_lazy("repeaters:list")

    def form_valid(self, form):
        form.instance.source = Repeater.Source.MANUAL
        form.instance.created_by = self.request.user
        response = super().form_valid(form)
        _audit(self.request, f"Created repeater {self.object.callsign}", self.object)
        messages.success(self.request, _("Repeater created."))
        return response

    def get_context_data(self, **kwargs):
        return super().get_context_data(form_title=_("New repeater"), **kwargs)


class RepeaterUpdateView(AreaPermissionMixin, ManageRequiredMixin, ManualOnlyMixin, UpdateView):
    area = "repeaters"
    verbs = {"GET": "view", "POST": "view"}
    model = Repeater
    form_class = RepeaterForm
    template_name = "repeaters/repeater_form.html"
    success_url = reverse_lazy("repeaters:list")

    def form_valid(self, form):
        def _plain(value):
            # Foreign keys (replaces) compare by pk on both sides.
            return str(getattr(value, "pk", value) if value is not None else "")

        changes = {
            name: [_plain(form.initial.get(name)), _plain(form.cleaned_data[name])]
            for name in form.changed_data
        }
        response = super().form_valid(form)
        _audit(self.request, f"Updated repeater {self.object.callsign}", self.object, changes)
        messages.success(self.request, _("Repeater saved."))
        return response

    def get_context_data(self, **kwargs):
        return super().get_context_data(form_title=_("Edit repeater"), **kwargs)


class RepeaterDeactivateView(AreaPermissionMixin, ManageRequiredMixin, TemplateView):
    """GET: confirm page. POST ``target=deactivate|reactivate`` sets ``active`` explicitly."""

    area = "repeaters"
    verbs = {"GET": "view", "POST": "view"}
    template_name = "repeaters/repeater_confirm_deactivate.html"

    def get_repeater(self):
        repeater = get_object_or_404(Repeater, pk=self.kwargs["pk"])
        if repeater.source != Repeater.Source.MANUAL:
            raise PermissionDenied
        return repeater

    def get_context_data(self, **kwargs):
        return super().get_context_data(repeater=self.get_repeater(), **kwargs)

    def post(self, request, *args, **kwargs):
        repeater = self.get_repeater()
        target = request.POST.get("target")
        if target not in ("deactivate", "reactivate"):
            return HttpResponseBadRequest("target must be deactivate or reactivate")
        new_active = target == "reactivate"
        if repeater.active != new_active:
            old = repeater.active
            repeater.active = new_active
            repeater.save(update_fields=["active", "updated_at"])
            verb = "Reactivated" if new_active else "Deactivated"
            _audit(
                request,
                f"{verb} repeater {repeater.callsign}",
                repeater,
                {"active": [str(old), str(new_active)]},
            )
        messages.success(
            request,
            _("Repeater reactivated.") if repeater.active else _("Repeater deactivated."),
        )
        return redirect(reverse("repeaters:list"))


MAX_SCAN_LISTS = 50
MAX_ENTRIES_PER_LIST = 200


class ScanListMixin(AreaPermissionMixin):
    """Scan lists belong to their owner and need ``control.use``; others get a 404."""

    area = "control"
    verbs = {"GET": "use", "POST": "use"}

    def get_scan_list(self):
        return get_object_or_404(ScanList, pk=self.kwargs["pk"], owner=self.request.user)


class ScanListIndexView(ScanListMixin, ListView):
    template_name = "repeaters/scanlist_list.html"
    context_object_name = "scan_lists"

    def get_queryset(self):
        return ScanList.objects.filter(owner=self.request.user).annotate(count=Count("entries"))


class ScanListMineJsonView(ScanListMixin, View):
    def get(self, request):
        lists = ScanList.objects.filter(owner=request.user).annotate(count=Count("entries"))
        return JsonResponse(
            [{"id": sl.pk, "name": sl.name, "count": sl.count} for sl in lists], safe=False
        )


class ScanListCreateView(ScanListMixin, CreateView):
    model = ScanList
    form_class = ScanListForm
    template_name = "repeaters/scanlist_form.html"

    def post(self, request, *args, **kwargs):
        if ScanList.objects.filter(owner=request.user).count() >= MAX_SCAN_LISTS:
            messages.error(
                request,
                _("You can have at most %(n)d scan lists. Delete one first.")
                % {"n": MAX_SCAN_LISTS},
            )
            return redirect("repeaters:scanlists")
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("repeaters:scanlist_detail", args=[self.object.pk])

    def get_context_data(self, **kwargs):
        return super().get_context_data(form_title=_("New scan list"), **kwargs)


class ScanListDetailView(ScanListMixin, TemplateView):
    """Entries of one list; GET shows them, POST renames the list."""

    template_name = "repeaters/scanlist_detail.html"

    def get_context_data(self, **kwargs):
        scan_list = self.get_scan_list()
        kwargs.setdefault("rename_form", ScanListForm(instance=scan_list))
        kwargs.setdefault("entry_form", ScanListEntryForm())
        return super().get_context_data(
            scan_list=scan_list,
            entries=scan_list.entries.select_related("repeater"),
            **kwargs,
        )

    def post(self, request, *args, **kwargs):
        form = ScanListForm(request.POST, instance=self.get_scan_list())
        if form.is_valid():
            form.save()
            messages.success(request, _("Scan list renamed."))
            return redirect(request.path)
        return self.render_to_response(self.get_context_data(rename_form=form))


class ScanListDeleteView(ScanListMixin, TemplateView):
    template_name = "repeaters/scanlist_confirm_delete.html"

    def get_context_data(self, **kwargs):
        return super().get_context_data(scan_list=self.get_scan_list(), **kwargs)

    def post(self, request, *args, **kwargs):
        self.get_scan_list().delete()
        messages.success(request, _("Scan list deleted."))
        return redirect("repeaters:scanlists")


def _renumber(scan_list):
    """Dense 0..n-1 positions; per-instance saves so entry signals fire."""
    for index, entry in enumerate(scan_list.entries.all()):
        if entry.position != index:
            entry.position = index
            entry.save(update_fields=["position"])


class ScanListEntryAddView(ScanListMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk):
        scan_list = self.get_scan_list()
        if scan_list.entries.count() >= MAX_ENTRIES_PER_LIST:
            messages.error(
                request,
                _("A scan list holds at most %(n)d entries.") % {"n": MAX_ENTRIES_PER_LIST},
            )
            return redirect("repeaters:scanlist_detail", pk=scan_list.pk)
        form = ScanListEntryForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.scan_list = scan_list
            last = scan_list.entries.aggregate(m=Max("position"))["m"]
            entry.position = 0 if last is None else last + 1
            entry.save()
            messages.success(request, _("Entry added."))
        else:
            errors = "; ".join(e for errs in form.errors.values() for e in errs)
            messages.error(request, errors)
        return redirect("repeaters:scanlist_detail", pk=scan_list.pk)


class ScanListEntryDeleteView(ScanListMixin, View):
    http_method_names = ["post"]

    def post(self, request, pk, entry_pk):
        scan_list = self.get_scan_list()
        get_object_or_404(ScanListEntry, pk=entry_pk, scan_list=scan_list).delete()
        _renumber(scan_list)
        messages.success(request, _("Entry removed."))
        return redirect("repeaters:scanlist_detail", pk=scan_list.pk)


class ScanListEntryMoveView(ScanListMixin, View):
    """POST ``direction=up|down``: swap with the neighbour."""

    http_method_names = ["post"]

    def post(self, request, pk, entry_pk):
        scan_list = self.get_scan_list()
        direction = request.POST.get("direction")
        if direction not in ("up", "down"):
            return HttpResponseBadRequest("direction must be up or down")
        get_object_or_404(ScanListEntry, pk=entry_pk, scan_list=scan_list)
        _renumber(scan_list)
        entries = list(scan_list.entries.all())
        index = next(i for i, e in enumerate(entries) if e.pk == entry_pk)
        other = index - 1 if direction == "up" else index + 1
        if 0 <= other < len(entries):
            a, b = entries[index], entries[other]
            a.position, b.position = b.position, a.position
            a.save(update_fields=["position"])
            b.save(update_fields=["position"])
        return redirect("repeaters:scanlist_detail", pk=scan_list.pk)
