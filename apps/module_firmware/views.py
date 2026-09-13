from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.views import View
from django.views.generic import DetailView, ListView

from . import services
from .models import Module


class ModuleListView(LoginRequiredMixin, ListView):
    model = Module
    template_name = "module_firmware/module_list.html"
    context_object_name = "modules"
    paginate_by = 50

    def get_queryset(self):
        qs = Module.objects.select_related("module_type").prefetch_related(
            "station_modules__station"
        )
        g = self.request.GET
        if g.get("type"):
            qs = qs.filter(module_type__key=g["type"])
        if g.get("lifecycle"):
            qs = qs.filter(lifecycle_status=g["lifecycle"])
        if g.get("registration"):
            qs = qs.filter(registration_status=g["registration"])
        if g.get("uid_source"):
            qs = qs.filter(uid_source=g["uid_source"])
        if g.get("station"):
            qs = qs.filter(station_modules__station_id=g["station"]).distinct()
        if g.get("q"):
            qs = qs.filter(Q(uid__icontains=g["q"]))
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["lifecycles"] = Module.Lifecycle.choices
        ctx["registrations"] = Module.Registration.choices
        ctx["uid_sources"] = Module.UidSource.choices
        ctx["total_count"] = Module.objects.count()
        ctx["unregistered_count"] = Module.objects.filter(
            registration_status=Module.Registration.UNREGISTERED
        ).count()
        g = self.request.GET
        ctx["filters"] = {
            "type": g.get("type", ""),
            "lifecycle": g.get("lifecycle", ""),
            "registration": g.get("registration", ""),
            "uid_source": g.get("uid_source", ""),
            "q": g.get("q", ""),
        }
        return ctx


class ModuleDetailView(LoginRequiredMixin, DetailView):
    model = Module
    template_name = "module_firmware/module_detail.html"
    context_object_name = "module"
    slug_field = "uid"
    slug_url_kwarg = "uid"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        m = self.object
        ctx["assignments"] = m.assignments.select_related("station").order_by("-from_ts")
        ctx["current_assignment"] = m.assignments.filter(to_ts__isnull=True).first()
        ctx["audit_logs"] = m.audit_logs.select_related("station", "user").all()[:200]
        ctx["can_edit"] = self.request.user.is_staff
        ctx["lifecycles"] = Module.Lifecycle.choices
        return ctx


class _StaffModuleMixin(LoginRequiredMixin, UserPassesTestMixin):
    raise_exception = True

    def test_func(self):
        return self.request.user.is_staff

    def get_module(self):
        return get_object_or_404(Module, uid=self.kwargs["uid"])

    def _redirect(self, uid):
        return HttpResponseRedirect(reverse("module_firmware:module_detail", args=[uid]))


class ModuleConfirmView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        services.confirm_registration(m, user=request.user)
        return self._redirect(uid)


class ModuleLifecycleView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        status = request.POST.get("lifecycle_status")
        if status in dict(Module.Lifecycle.choices):
            services.set_lifecycle(m, status, user=request.user)
        return self._redirect(uid)


class ModuleNotesView(_StaffModuleMixin, View):
    def post(self, request, uid):
        m = self.get_module()
        m.notes = request.POST.get("notes", "")
        m.save(update_fields=["notes", "updated_at"])
        return self._redirect(uid)
