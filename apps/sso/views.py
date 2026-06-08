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
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
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
        for g in AppGrant.objects.filter(user=user, revoked_at__isnull=True).select_related(
            "application"
        )
    }
    return [(app, active_grants.get(app.pk)) for app in Application.objects.order_by("name")]


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

        # Atomic toggle: serialize against double-submit/concurrent
        # clicks. Without this, two requests can both find no active
        # grant and both call .create() — the partial unique index
        # then 500s the loser. Wrap the read+write in a single
        # transaction with row locking on existing rows; if the
        # create still races (different worker, no lock yet), catch
        # IntegrityError and treat it as "the other request already
        # created the grant" → idempotent no-op.
        #
        # `audit_event` is set only when this request actually changed
        # state. If we lost the create race, the other request will
        # write its own audit row; ours stays silent to avoid double-
        # logging a no-op as a real grant transition.
        audit_event = None
        audit_verb = None
        with transaction.atomic():
            active = (
                AppGrant.objects.select_for_update()
                .filter(user=target, application=application, revoked_at__isnull=True)
                .first()
            )

            if active is not None:
                active.revoked_at = timezone.now()
                active.save(update_fields=["revoked_at"])
                audit_event = SsoAuditLog.EventType.GRANT_REVOKED
                audit_verb = _("revoked")
            else:
                try:
                    AppGrant.objects.create(
                        user=target,
                        application=application,
                        granted_by=request.user,
                    )
                except IntegrityError:
                    # Lost the race — another request just created
                    # the grant. State is now identical to the
                    # winning request's; skip the audit write so the
                    # log doesn't claim two GRANT_GIVEN events for
                    # one transition.
                    logger.info(
                        "GrantToggleView lost create race for user=%s app=%s; "
                        "no audit row written (winner already logged it)",
                        target.username,
                        application.client_id,
                    )
                else:
                    audit_event = SsoAuditLog.EventType.GRANT_GIVEN
                    audit_verb = _("granted")

        if audit_event is not None:
            SsoAuditLog.log(
                event_type=audit_event,
                actor=request.user,
                target_user=target,
                application=application,
                message=f"{audit_verb} via toggle UI",
                ip_address=_client_ip(request),
            )

        # If the toggle was triggered from the application-detail page
        # (signalled via HX-Trigger-Name=from-app-detail), respond with
        # HX-Redirect so the whole page refreshes — partial swap can't
        # update both the "users_with_grant" and "users_without_grant"
        # columns at once from a single button click.
        if request.headers.get("HX-Trigger-Name") == "from-app-detail":
            resp = HttpResponse(status=200)
            resp["HX-Redirect"] = reverse("sso:application_detail", kwargs={"pk": application.pk})
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
        # Annotate the grant count in a single query instead of doing
        # one COUNT per Application in a Python loop (N+1).
        return Application.objects.annotate(
            active_grant_count=Count(
                "grants",
                filter=Q(grants__revoked_at__isnull=True),
            ),
        ).order_by("name")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Fleet-wide active grant total powers the KPI tile; cheap
        # aggregate since AppGrant is a small table.
        ctx["active_grants_total"] = AppGrant.objects.filter(
            revoked_at__isnull=True,
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
            application=self.object,
            revoked_at__isnull=True,
        ).values_list("user_id", flat=True)
        ctx["users_with_grant"] = User.objects.filter(pk__in=active_user_ids).order_by("username")
        ctx["users_without_grant"] = User.objects.exclude(pk__in=active_user_ids).order_by(
            "username"
        )
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
        client_id = request.GET.get("client_id") or request.POST.get("client_id")
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

        # Decide the audit event and error_description: inactive user
        # vs. missing grant. We intentionally surface the distinction
        # in the RFC error_description so RP-side logs name the right
        # cause; both denials are operationally-meaningful so leaking
        # the policy difference is acceptable for an internal IdP.
        if not getattr(request.user, "is_active", False):
            event_type = SsoAuditLog.EventType.LOGIN_DENIED_INACTIVE
            error_description = "User account is inactive."
        else:
            event_type = SsoAuditLog.EventType.LOGIN_DENIED_NO_GRANT
            error_description = "User has no active grant for this application."

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
        # Keep parse_qsl's list-of-pairs form (don't collapse via dict()):
        # the original redirect_uri may legitimately repeat a query key,
        # and the RFC error-redirect should preserve that structure
        # rather than silently drop duplicates. urlencode(..., doseq=True)
        # round-trips the list back into a query string.
        parsed = urlparse(redirect_uri)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        pairs.append(("error", "access_denied"))
        pairs.append(("error_description", error_description))
        if state:
            pairs.append(("state", state))
        new_query = urlencode(pairs, doseq=True)
        target = urlunparse(parsed._replace(query=new_query))
        return HttpResponseRedirect(target)


def _is_registered_redirect(application, candidate_uri: str) -> bool:
    """Delegate to DOT's own redirect-URI matcher.

    Earlier revisions of this code did an exact-string compare against
    the whitespace-split list; that diverged from DOT's happy-path
    validator (which normalizes via oauthlib's
    ``redirect_to_uri_allowed`` and tolerates extra query-string
    parameters per the RFC). Behavioural drift between the deny path
    and the happy path caused spurious 400s on URIs DOT itself would
    have accepted on success. Reuse the library function so both
    paths agree.

    Returns False for any miss; the caller falls back to 400.
    """
    if application is None or not candidate_uri:
        return False
    return application.redirect_uri_allowed(candidate_uri)


# ---------------------------------------------------------------------------
# Task 5.1: SessionRevokeView
# ---------------------------------------------------------------------------


class SessionRevokeView(AdminOnlyMixin, View):
    """POST-only: revoke a single TokenSession + its RefreshToken.

    Idempotent: a second POST on an already-revoked session is a no-op
    (no extra audit row, no extra DOT-token mutation). Spec §4.4.
    """

    def post(self, request, pk):
        from datetime import timedelta

        from oauth2_provider.models import AccessToken

        from .models import TokenSession

        session = get_object_or_404(TokenSession, pk=pk)
        if session.revoked_at is None:
            now = timezone.now()
            with transaction.atomic():
                rt = session.refresh_token
                if rt is not None and rt.revoked is None:
                    rt.revoked = now
                    rt.save(update_fields=["revoked"])
                    # Expire any AccessTokens tied to this session so
                    # the client can't keep using a still-valid AT
                    # after the RT is gone. We include both:
                    #  - rotated ATs (source_refresh_token=rt), and
                    #  - the original AT this RT was issued alongside
                    #    (rt.access_token), which DOT does NOT mark
                    #    with source_refresh_token and would otherwise
                    #    survive until natural expiry (~1h).
                    # Spec §4.4 requires killing ALL access for this
                    # session.
                    AccessToken.objects.filter(
                        Q(source_refresh_token=rt) | Q(pk=rt.access_token_id),
                    ).update(expires=now - timedelta(seconds=1))

                session.revoked_at = now
                session.revoked_by = request.user
                session.revoke_reason = TokenSession.RevokeReason.ADMIN_REVOKE
                session.save(
                    update_fields=["revoked_at", "revoked_by", "revoke_reason"],
                )

            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.SESSION_REVOKED,
                actor=request.user,
                target_user=session.user,
                application=session.application,
                message=(
                    f"Session {session.pk} revoked. "
                    f"Issued {session.issued_at.isoformat()} "
                    f"from {session.ip_address} ({session.city or 'unknown'})"
                ),
                ip_address=_client_ip(request),
            )

        # HTMX vs. standard browser response. The partial template
        # ``sso/_sessions_card.html`` lands in Task 6.1; until then the
        # HTMX branch will TemplateDoesNotExist — fine, no caller yet.
        if getattr(request, "htmx", False):
            return render(
                request,
                "sso/_sessions_card.html",
                {
                    "target_user": session.user,
                    "sessions": _active_sessions_for(session.user),
                },
            )
        return HttpResponseRedirect(
            request.META.get("HTTP_REFERER", reverse("sso:dashboard")),
        )


def _active_sessions_for(user):
    """Active TokenSessions for a user, newest first.

    Used by the user-form template (Task 6.1) and by the HTMX swap
    response from ``SessionRevokeView``.
    """
    from .models import TokenSession

    return (
        TokenSession.objects.filter(user=user, revoked_at__isnull=True)
        .select_related("application")
        .order_by("-last_seen_at")
    )


# ---------------------------------------------------------------------------
# Task 5.2: ApplicationPolicyUpdateView
# ---------------------------------------------------------------------------


class ApplicationPolicyUpdateView(AdminOnlyMixin, View):
    """POST-only: set or update the ApplicationPolicy for an Application.

    Auto-creates the policy row on first set; existing-row update emits
    APP_POLICY_CHANGED audit with snapshot of affected active sessions
    at the time of the change. Spec §3.4.
    """

    def post(self, request, pk):
        from .models import ApplicationPolicy, TokenSession

        application = get_object_or_404(Application, pk=pk)
        new_policy = request.POST.get("access_policy", "")
        valid_choices = {v for v, _ in ApplicationPolicy.AccessPolicy.choices}
        if new_policy not in valid_choices:
            return HttpResponseBadRequest("invalid access_policy value")

        pol, created = ApplicationPolicy.objects.get_or_create(
            application=application,
            defaults={"access_policy": new_policy, "modified_by": request.user},
        )
        old_policy = pol.access_policy if not created else "grant_required"

        policy_changed = created or (old_policy != new_policy)
        if not created and pol.access_policy != new_policy:
            pol.access_policy = new_policy
            pol.modified_by = request.user
            pol.save(update_fields=["access_policy", "modified_by", "updated_at"])

        if policy_changed:
            active_session_count = TokenSession.objects.filter(
                application=application,
                revoked_at__isnull=True,
            ).count()
            SsoAuditLog.log(
                event_type=SsoAuditLog.EventType.APP_POLICY_CHANGED,
                actor=request.user,
                application=application,
                message=(
                    f"Policy {old_policy} -> {new_policy}. "
                    f"{active_session_count} active session(s) at the time of change."
                ),
                ip_address=_client_ip(request),
            )

        return HttpResponseRedirect(
            reverse("sso:application_detail", kwargs={"pk": application.pk}),
        )
