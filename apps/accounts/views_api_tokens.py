from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View

from apps.accounts.permissions import AreaPermissionMixin
from apps.api.models import PersonalAccessToken


def api_tokens_tab_url(user):
    """Token management lives as a tab on the user's own detail page."""
    return reverse("accounts:user_detail", kwargs={"pk": user.pk}) + "#api-tokens"


class ApiTokenListView(AreaPermissionMixin, View):
    area = "api_tokens"
    verb = "use"

    def get(self, request):
        # Old standalone page; kept as a redirect so bookmarks keep working.
        return redirect(api_tokens_tab_url(request.user))

    def _render_error(self, request, message):
        messages.error(request, message)
        return redirect(api_tokens_tab_url(request.user))

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


class ApiTokenRevokeView(AreaPermissionMixin, View):
    area = "api_tokens"
    verb = "use"

    def post(self, request, pk):
        token = get_object_or_404(PersonalAccessToken, pk=pk, user=request.user)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
        return redirect(api_tokens_tab_url(request.user))
