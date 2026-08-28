from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.urls import reverse_lazy
from django.views.generic import UpdateView

from .forms_notifications import NotificationChannelForm


class NotificationSettingsView(LoginRequiredMixin, UpdateView):
    form_class = NotificationChannelForm
    template_name = "accounts/notification_settings.html"
    success_url = reverse_lazy("accounts:notification_settings")

    def get_object(self):
        return self.request.user

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["vapid_public_key"] = settings.WEBPUSH_VAPID_PUBLIC_KEY
        ctx["webpush_enabled"] = settings.ALERT_WEBPUSH_ENABLED
        ctx["subscriptions"] = self.request.user.push_subscriptions.all()
        return ctx
