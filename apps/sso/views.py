"""Custom UI for the SSO/OIDC provider — admin-only.

GrantToggleView is the workhorse for T13: flips an AppGrant active/
revoked for (user, application). Idempotent given the same starting
state, emits SsoAuditLog per transition.

SsoDashboardView (T14 template) and ApplicationDetailView (T15
template) are declared here so URLs can resolve; their actual
template files land in subsequent tasks.
"""

import logging

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView
from oauth2_provider.models import Application

from .models import AppGrant, SsoAuditLog

User = get_user_model()
logger = logging.getLogger(__name__)


class AdminOnlyMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Only admins may access SSO management views.

    Anonymous callers get the standard LoginRequiredMixin redirect to
    ``LOGIN_URL`` (302). Authenticated-but-not-authorized callers get
    a PermissionDenied (403) — re-auth wouldn't help them, they need
    different permissions. We can't just set ``raise_exception=True``
    because AccessMixin applies it for *both* failure modes, which
    would 403 anonymous users instead of redirecting them. So we
    override ``handle_no_permission`` and branch on auth state.
    """

    def test_func(self):
        return getattr(self.request.user, "is_admin", False)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            raise PermissionDenied(self.get_permission_denied_message())
        return super().handle_no_permission()


def _client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    return xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR")


def _build_grants_for_user(user):
    """Return list of (Application, AppGrant|None) tuples for the card render.

    None in the AppGrant slot means the user does NOT currently have an
    active grant for that application.
    """
    active_grants = {
        g.application_id: g
        for g in AppGrant.objects.filter(
            user=user, revoked_at__isnull=True
        ).select_related("application")
    }
    return [
        (app, active_grants.get(app.pk))
        for app in Application.objects.order_by("name")
    ]


class GrantToggleView(AdminOnlyMixin, View):
    """POST-only: flip an AppGrant on or off for (user, application).

    Idempotent given the same starting state, with audit log entry per
    transition.

    Re-renders the grants card for HTMX swap. Standard browser POST
    (non-HTMX) falls back to the same partial — fine for now; T15 may
    add a redirect-to-app-detail path.
    """

    def post(self, request, user_id, application_id):
        target = get_object_or_404(User, pk=user_id)
        application = get_object_or_404(Application, pk=application_id)

        active = AppGrant.objects.filter(
            user=target, application=application, revoked_at__isnull=True
        ).first()

        if active is not None:
            active.revoked_at = timezone.now()
            active.save(update_fields=["revoked_at"])
            event_type = SsoAuditLog.EventType.GRANT_REVOKED
            verb = _("revoked")
        else:
            AppGrant.objects.create(
                user=target,
                application=application,
                granted_by=request.user,
            )
            event_type = SsoAuditLog.EventType.GRANT_GIVEN
            verb = _("granted")

        SsoAuditLog.log(
            event_type=event_type,
            actor=request.user,
            target_user=target,
            application=application,
            message=f"{verb} via toggle UI",
            ip_address=_client_ip(request),
        )

        return render(
            request,
            "sso/_app_grants_card.html",
            {
                "target_user": target,
                "applications": _build_grants_for_user(target),
            },
        )


class SsoDashboardView(AdminOnlyMixin, ListView):
    """Top-level SSO overview — list registered apps + grant counts.

    Template lands in T14.
    """

    template_name = "sso/dashboard.html"
    context_object_name = "applications"

    def get_queryset(self):
        return Application.objects.order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        for app in ctx["applications"]:
            app.active_grant_count = AppGrant.objects.filter(
                application=app, revoked_at__isnull=True
            ).count()
        return ctx


class ApplicationDetailView(AdminOnlyMixin, DetailView):
    """Per-application detail page. Template lands in T15."""

    template_name = "sso/application_detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return Application.objects.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        active_user_ids = AppGrant.objects.filter(
            application=self.object, revoked_at__isnull=True,
        ).values_list("user_id", flat=True)
        ctx["users_with_grant"] = User.objects.filter(
            pk__in=active_user_ids
        ).order_by("username")
        ctx["users_without_grant"] = User.objects.exclude(
            pk__in=active_user_ids
        ).order_by("username")
        return ctx
