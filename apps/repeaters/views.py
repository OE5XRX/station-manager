from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext as _
from django.views.generic import CreateView, ListView, TemplateView, UpdateView

from apps.accounts.client_ip import client_ip
from apps.accounts.models import AccountAuditLog
from apps.accounts.permissions import AreaPermissionMixin

from .forms import RepeaterForm
from .models import Repeater
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
        changes = {
            name: [str(form.initial.get(name) or ""), str(form.cleaned_data[name] or "")]
            for name in form.changed_data
        }
        response = super().form_valid(form)
        _audit(self.request, f"Updated repeater {self.object.callsign}", self.object, changes)
        messages.success(self.request, _("Repeater saved."))
        return response

    def get_context_data(self, **kwargs):
        return super().get_context_data(form_title=_("Edit repeater"), **kwargs)


class RepeaterDeactivateView(AreaPermissionMixin, ManageRequiredMixin, TemplateView):
    """GET: confirm page. POST: toggle ``active`` (deactivate / reactivate)."""

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
        repeater.active = not repeater.active
        repeater.save(update_fields=["active", "updated_at"])
        verb = "Reactivated" if repeater.active else "Deactivated"
        _audit(
            request,
            f"{verb} repeater {repeater.callsign}",
            repeater,
            {"active": [str(not repeater.active), str(repeater.active)]},
        )
        messages.success(
            request,
            _("Repeater reactivated.") if repeater.active else _("Repeater deactivated."),
        )
        return redirect(reverse("repeaters:list"))
