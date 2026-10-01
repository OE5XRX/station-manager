from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View

from apps.api.models import PersonalAccessToken


class ApiTokenListView(LoginRequiredMixin, View):
    template_name = "accounts/api_tokens.html"

    def get(self, request):
        tokens = PersonalAccessToken.objects.filter(user=request.user)
        return render(request, self.template_name, {"tokens": tokens})

    def post(self, request):
        name = (request.POST.get("name") or "").strip()
        if not name:
            tokens = PersonalAccessToken.objects.filter(user=request.user)
            return render(
                request,
                self.template_name,
                {"tokens": tokens, "error": "Name ist erforderlich."},
            )
        token, raw = PersonalAccessToken.issue(request.user, name=name)
        return render(
            request,
            "accounts/api_token_created.html",
            {"token": token, "raw_token": raw},
        )


class ApiTokenRevokeView(LoginRequiredMixin, View):
    def post(self, request, pk):
        token = get_object_or_404(PersonalAccessToken, pk=pk, user=request.user)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
        return redirect(reverse("accounts:api_tokens"))
