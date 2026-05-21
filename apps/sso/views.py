"""Custom UI for the SSO/OIDC provider — admin-only.

GrantToggleView is the workhorse for T13: flips an AppGrant active/
revoked for (user, application). Idempotent given the same starting
state, emits SsoAuditLog per transition.

SsoDashboardView (T14 template) and ApplicationDetailView (T15
template) are declared here so URLs can resolve; their actual
template files land in subsequent tasks.
"""

import logging
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView
from oauth2_provider.models import Application
from oauth2_provider.views import AuthorizationView as DotAuthorizationView

from .models import AppGrant, SsoAuditLog
from .permissions import user_can_access

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

        # If the toggle was triggered from the application-detail page
        # (signalled via HX-Trigger-Name=from-app-detail), respond with
        # HX-Redirect so the whole page refreshes — partial swap can't
        # update both the "users_with_grant" and "users_without_grant"
        # columns at once from a single button click.
        if request.headers.get("HX-Trigger-Name") == "from-app-detail":
            resp = HttpResponse(status=200)
            resp["HX-Redirect"] = reverse(
                "sso:application_detail", kwargs={"pk": application.pk}
            )
            return resp

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


# ---------------------------------------------------------------------------
# AppGrant gate on the authorization endpoint
# ---------------------------------------------------------------------------


class AppGrantAuthorizationView(DotAuthorizationView):
    """DOT AuthorizationView wrapped with the AppGrant access gate.

    Two-line summary:
    - Pre-flight: resolve client_id -> Application, check user_can_access.
    - On deny: redirect back to RP with ?error=access_denied&state=...
      WITHOUT calling super().dispatch(), so no code/token is ever issued.

    The check must happen BEFORE DOT renders the consent screen — otherwise
    a denied user sees a "Authorize InvenTree" page that does nothing on
    submit, which is confusing UX. By short-circuiting in dispatch(), the
    denied user is bounced straight back to the RP.

    Audit log:
    - LOGIN_DENIED_NO_GRANT: authenticated, no active AppGrant.
    - LOGIN_DENIED_INACTIVE: user.is_active is False.
    Both events are written best-effort; an audit failure must NOT alter
    the security outcome (still deny).
    """

    def dispatch(self, request, *args, **kwargs):
        # Only gate the actual flow, not anonymous GET/POST that DOT will
        # reject anyway. If the user isn't authenticated, fall through so
        # DOT redirects to the login page via the LoginRequiredMixin it
        # already wraps the view in.
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)

        # client_id arrives either as a GET param (initial visit + consent
        # POST) or implicitly via session for some DOT versions. Read both.
        client_id = (
            request.GET.get("client_id")
            or request.POST.get("client_id")
        )
        if not client_id:
            # No client_id means the request is malformed; let DOT handle
            # the 400.
            return super().dispatch(request, *args, **kwargs)

        application = Application.objects.filter(client_id=client_id).first()
        if application is None:
            # Unknown application — let DOT handle the error (does NOT
            # redirect since redirect_uri may also be untrusted).
            return super().dispatch(request, *args, **kwargs)

        if user_can_access(request.user, application):
            return super().dispatch(request, *args, **kwargs)

        # --- Denied path -----------------------------------------------------

        # Decide the audit event: inactive user vs. missing grant.
        if not getattr(request.user, "is_active", False):
            event_type = SsoAuditLog.EventType.LOGIN_DENIED_INACTIVE
        else:
            event_type = SsoAuditLog.EventType.LOGIN_DENIED_NO_GRANT

        try:
            SsoAuditLog.log(
                event_type=event_type,
                actor=request.user,
                target_user=request.user,
                application=application,
                message=(
                    f"OIDC authorize denied: {event_type.label}. "
                    f"User={request.user.username} App={application.client_id}"
                ),
                ip_address=_client_ip(request),
            )
        except Exception:
            logger.exception("Audit log write failed during authorize deny")

        # Pull the requested redirect_uri + state for the RP-bounce. The
        # redirect_uri MUST be one the Application has registered — else
        # we have an open-redirect; fall back to a 400 in that case.
        redirect_uri = request.GET.get("redirect_uri") or request.POST.get("redirect_uri")
        state = request.GET.get("state") or request.POST.get("state") or ""

        if not redirect_uri or not _is_registered_redirect(application, redirect_uri):
            # Cannot safely bounce back. Return 400 without echoing the
            # untrusted URI back to the user.
            return HttpResponseBadRequest("access_denied")

        # Append the error params to the redirect URI's query string.
        parsed = urlparse(redirect_uri)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        existing["error"] = "access_denied"
        existing["error_description"] = "User has no active grant for this application."
        if state:
            existing["state"] = state
        new_query = urlencode(existing)
        target = urlunparse(parsed._replace(query=new_query))
        return HttpResponseRedirect(target)


def _is_registered_redirect(application, candidate_uri: str) -> bool:
    """Exact-string match against application.redirect_uris.

    Whitespace-separated list; `candidate_uri` must match one element
    character-for-character. NO normalization (trailing slash, host
    casing, percent-encoding) — operators registering URIs must use
    the canonical form they want clients to send. Strict by design,
    mirrors DOT's own validator: any divergence would create an
    open-redirect surface (an RP sending an unexpected form gets
    bounced to whatever the partial match resolves to).

    Returns False for any miss; the caller falls back to 400.
    """
    registered = (application.redirect_uris or "").split()
    return candidate_uri in registered
