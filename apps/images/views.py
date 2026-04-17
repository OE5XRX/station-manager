from django.contrib import messages
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, ListView

from apps.accounts.views import AdminRequiredMixin

from .forms import ImageImportForm
from .models import ImageImportJob, ImageRelease


class ImageListView(AdminRequiredMixin, ListView):
    model = ImageRelease
    template_name = "images/image_list.html"
    context_object_name = "releases"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["import_form"] = ImageImportForm()
        ctx["recent_jobs"] = ImageImportJob.objects.order_by("-created_at")[:10]
        return ctx


class ImageImportView(AdminRequiredMixin, FormView):
    form_class = ImageImportForm
    template_name = "images/image_list.html"
    success_url = reverse_lazy("images:list")

    def form_valid(self, form):
        ImageImportJob.objects.create(
            tag=form.cleaned_data["tag"],
            machine=form.cleaned_data["machine"],
            mark_as_latest=form.cleaned_data["mark_as_latest"],
            requested_by=self.request.user,
        )
        messages.success(
            self.request,
            _("Import queued. It will appear below in a minute or two."),
        )
        return super().form_valid(form)
