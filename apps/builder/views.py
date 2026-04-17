from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.stations.views import AdminOrOperatorRequiredMixin

from .forms import BuildConfigForm
from .models import BuildConfig, BuildJob

# ---------------------------------------------------------------------------
# BuildConfig views
# ---------------------------------------------------------------------------


class BuildConfigListView(AdminOrOperatorRequiredMixin, ListView):
    model = BuildConfig
    template_name = "builder/buildconfig_list.html"
    context_object_name = "configs"
    paginate_by = 25

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("station", "tag", "base_image", "created_by")
            .prefetch_related("jobs")
        )


class BuildConfigCreateView(AdminOrOperatorRequiredMixin, CreateView):
    model = BuildConfig
    form_class = BuildConfigForm
    template_name = "builder/buildconfig_form.html"

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, _("Build config created successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("builder:buildconfig_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create Build Config")
        return context


class BuildConfigDetailView(AdminOrOperatorRequiredMixin, DetailView):
    model = BuildConfig
    template_name = "builder/buildconfig_detail.html"
    context_object_name = "config"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("station", "tag", "base_image", "created_by")
            .prefetch_related(
                "extra_firmware",
                "jobs__output_artifact",
                "jobs__created_by",
            )
        )


class BuildConfigUpdateView(AdminOrOperatorRequiredMixin, UpdateView):
    model = BuildConfig
    form_class = BuildConfigForm
    template_name = "builder/buildconfig_form.html"

    def form_valid(self, form):
        messages.success(self.request, _("Build config updated successfully."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("builder:buildconfig_detail", kwargs={"pk": self.object.pk})

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit Build Config")
        return context


# ---------------------------------------------------------------------------
# BuildJob views
# ---------------------------------------------------------------------------


class BuildJobListView(AdminOrOperatorRequiredMixin, ListView):
    model = BuildJob
    template_name = "builder/buildjob_list.html"
    context_object_name = "jobs"
    paginate_by = 25

    def get_queryset(self):
        qs = super().get_queryset().select_related("build_config", "output_artifact", "created_by")
        config_pk = self.request.GET.get("config")
        if config_pk:
            qs = qs.filter(build_config_id=config_pk)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        config_pk = self.request.GET.get("config")
        if config_pk:
            context["filter_config"] = BuildConfig.objects.filter(pk=config_pk).first()
        return context


class BuildJobDetailView(AdminOrOperatorRequiredMixin, DetailView):
    model = BuildJob
    template_name = "builder/buildjob_detail.html"
    context_object_name = "job"

    def get_queryset(self):
        return (
            super().get_queryset().select_related("build_config", "output_artifact", "created_by")
        )


class BuildJobTriggerView(AdminOrOperatorRequiredMixin, View):
    """Create a new pending BuildJob for the given BuildConfig."""

    def post(self, request, pk):
        config = get_object_or_404(BuildConfig, pk=pk)
        BuildJob.objects.create(
            build_config=config,
            status=BuildJob.Status.PENDING,
            created_by=request.user,
        )
        messages.success(
            request,
            _("Build queued. Yocto integration coming in a future update."),
        )
        return redirect("builder:buildconfig_detail", pk=pk)
