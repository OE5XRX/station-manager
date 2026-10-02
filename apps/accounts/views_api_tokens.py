from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.api.models import PersonalAccessToken


class ApiTokenListView(LoginRequiredMixin, View):
    template_name = "accounts/api_tokens.html"

    def get(self, request):
        tokens = PersonalAccessToken.objects.filter(user=request.user)
        return render(request, self.template_name, {"tokens": tokens})

    def _render_error(self, request, message):
        tokens = PersonalAccessToken.objects.filter(user=request.user)
        return render(
            request,
            self.template_name,
            {"tokens": tokens, "error": message},
        )

    def post(self, request):
        name = (request.POST.get("name") or "").strip()
        max_length = PersonalAccessToken._meta.get_field("name").max_length
        if not name:
            return self._render_error(request, _("Name ist erforderlich."))
        if len(name) > max_length:
            return self._render_error(
                request,
                _("Name darf höchstens %(max)d Zeichen lang sein.") % {"max": max_length},
            )
        token, raw = PersonalAccessToken.issue(request.user, name=name)
        response = render(
            request,
            "accounts/api_token_created.html",
            {"token": token, "raw_token": raw},
        )
        # The raw token is shown exactly once; prevent the browser from caching
        # or redisplaying this response from history on a shared/compromised client.
        response["Cache-Control"] = "no-store"
        return response


class ApiTokenRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(PersonalAccessToken, pk=pk, user=request.user)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
        return redirect(reverse("accounts:api_tokens"))
