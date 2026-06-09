"""Custom UI for the SSO/OIDC provider — admin-only.

GrantToggleView is the workhorse for T13: flips an AppGrant active/
revoked for (user, application). Idempotent given the same starting
state, emits SsoAuditLog per transition.

SsoDashboardView (T14 template) and ApplicationDetailView (T15
template) are declared here so URLs can resolve; their actual
template files land in subsequent tasks.
"""

import logging
import re
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
        # Annotate the grant count + session count in a single query
        # instead of doing one COUNT per Application in a Python loop
        # (N+1). Sessions feed the per-app column added in Task 5.4.
        # `distinct=True` on both Counts is critical: when Django generates
        # the JOIN to both `grants` and `token_sessions` tables, rows
        # multiply (M grants × N sessions). Without distinct, each Count
        # would over-report by the size of the *other* relation.
        return Application.objects.annotate(
            active_grant_count=Count(
                "grants",
                filter=Q(grants__revoked_at__isnull=True),
                distinct=True,
            ),
            active_session_count=Count(
                "token_sessions",
                filter=Q(token_sessions__revoked_at__isnull=True),
                distinct=True,
            ),
        ).order_by("name")

    def get_context_data(self, **kwargs):
        from .models import TokenSession

        ctx = super().get_context_data(**kwargs)
        # Fleet-wide active grant total powers the KPI tile; cheap
        # aggregate since AppGrant is a small table.
        ctx["active_grants_total"] = AppGrant.objects.filter(
            revoked_at__isnull=True,
        ).count()
        # Active session KPI (Task 5.4): both the global count and the
        # number of distinct apps with at least one active session
        # (drives the "across N app(s)" secondary line).
        ctx["active_sessions_total"] = TokenSession.objects.filter(
            revoked_at__isnull=True,
        ).count()
        ctx["active_sessions_apps"] = (
            TokenSession.objects.filter(revoked_at__isnull=True)
            .values("application")
            .distinct()
            .count()
        )
        return ctx


class ApplicationDetailView(AdminOnlyMixin, DetailView):
    """Per-application detail page. Template lands in T15."""

    template_name = "sso/application_detail.html"
    context_object_name = "application"

    def get_queryset(self):
        return Application.objects.all()

    def get_context_data(self, **kwargs):
        from django.contrib.auth.models import Group

        from apps.stations.models import RegionAssignment, StationAssignment

        from .models import ApplicationPolicy, TokenSession

        ctx = super().get_context_data(**kwargs)
        active_user_ids = AppGrant.objects.filter(
            application=self.object,
            revoked_at__isnull=True,
        ).values_list("user_id", flat=True)
        ctx["users_with_grant"] = User.objects.filter(pk__in=active_user_ids).order_by("username")
        ctx["users_without_grant"] = User.objects.exclude(pk__in=active_user_ids).order_by(
            "username"
        )

        # Task 6.4: Policy selector context
        ctx["policy"] = getattr(self.object, "sso_policy", None)
        ctx["policy_choices"] = ApplicationPolicy.AccessPolicy.choices
        ctx["current_policy_value"] = (
            ctx["policy"].access_policy if ctx["policy"] else "grant_required"
        )

        # Task 6.4: Preview list of all groups currently propagated for any
        # user in the system. Used for the "Group propagation" section.
        # Station has no slug field — use station.pk.
        #
        # Use ``values_list(..., distinct=True)`` so the DB de-duplicates
        # on the (id, role) / (slug, role) tuple before hydrating any
        # rows. The naïve form (iterating assignment objects and
        # de-duping in Python) scales O(N) with the assignment count
        # even if there are only a handful of distinct combinations.
        # Derive from the enum so the preview list never silently drifts
        # when User.MembershipLevel adds/removes a value.
        membership_levels = [v for v in User.MembershipLevel.values]
        station_groups = [
            f"station:{pk}:{role}"
            for pk, role in StationAssignment.objects.values_list("station_id", "role").distinct()
        ]
        region_groups = [
            f"region:{slug}:{role}"
            for slug, role in RegionAssignment.objects.values_list(
                "region__slug", "role"
            ).distinct()
        ]
        tag_groups = [f"tag:{n}" for n in Group.objects.values_list("name", flat=True)]
        ctx["propagated_group_strings"] = sorted(
            set(membership_levels + station_groups + region_groups + tag_groups)
        )

        # Task 6.4: Recent sessions on this app (last 50). We also
        # select_related("refresh_token") so the template's
        # ``s.is_active`` property check (added in the round-4 status
        # label refactor) doesn't trigger an N+1 fetch per row.
        ctx["recent_sessions"] = (
            TokenSession.objects.filter(
                application=self.object,
            )
            .select_related("user", "refresh_token")
            .order_by("-issued_at")[:50]
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

        # First fetch is just a 404 gate; the load-bearing check happens
        # under SELECT FOR UPDATE inside the transaction. This makes the
        # operation truly idempotent against concurrent double-clicks /
        # HTMX retries: two requests can both reach the atomic block,
        # but only the winner finds revoked_at IS NULL.
        get_object_or_404(TokenSession, pk=pk)
        now = timezone.now()
        already_revoked = False
        session = None
        with transaction.atomic():
            session = TokenSession.objects.select_for_update().get(pk=pk)
            if session.revoked_at is not None:
                already_revoked = True
            else:
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

        if not already_revoked:
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

        # HTMX vs. standard browser response. HTMX callers receive the
        # rendered ``sso/_sessions_card.html`` partial so the user-form
        # page can swap the card in place; plain browser POSTs follow
        # the safe-referer fallback below.
        if getattr(request, "htmx", False):
            return render(
                request,
                "sso/_sessions_card.html",
                {
                    "target_user": session.user,
                    "sessions": _active_sessions_for(session.user),
                },
            )
        # HTTP_REFERER is attacker-controllable, so validate it against
        # the request's own host before redirecting. Falls back to the
        # SSO dashboard for off-host or empty referers (open-redirect
        # safe).
        from django.utils.http import url_has_allowed_host_and_scheme

        referer = request.META.get("HTTP_REFERER", "")
        if referer and url_has_allowed_host_and_scheme(
            referer,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        ):
            target = referer
        else:
            target = reverse("sso:dashboard")
        return HttpResponseRedirect(target)


def _active_sessions_for(user):
    """Active TokenSessions for a user, newest first.

    Used by the user-form template (Task 6.1) and by the HTMX swap
    response from ``SessionRevokeView``. Delegates the "active"
    predicate to ``TokenSession.objects.active()`` so the criteria
    stay consistent across all call sites (admin UI, audit-log
    counters, etc.).
    """
    from .models import TokenSession

    return (
        TokenSession.objects.active()
        .filter(user=user)
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

        # NB: do not include ``created`` here. When the row is freshly
        # created with the implicit-default ``grant_required`` value
        # (admin posts the same value that was already in effect),
        # ``old_policy`` and ``new_policy`` both equal ``grant_required``
        # and no real change happened — emitting an audit row would be
        # misleading no-op noise.
        policy_changed = old_policy != new_policy
        if not created and pol.access_policy != new_policy:
            pol.access_policy = new_policy
            pol.modified_by = request.user
            pol.save(update_fields=["access_policy", "modified_by", "updated_at"])

        if policy_changed:
            # Use ``.active()`` so the audit log records the precise
            # number of *usable* sessions affected by the change (not
            # just rows with revoked_at IS NULL, which would include
            # lifetime-expired sessions). The KPI tile elsewhere uses
            # the simpler aggregate by design — but audit-log integrity
            # warrants the precise count here.
            active_session_count = (
                TokenSession.objects.active().filter(application=application).count()
            )
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


# ---------------------------------------------------------------------------
# Task 5.3: Tag management views
# ---------------------------------------------------------------------------


_TAG_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class TagListView(AdminOnlyMixin, ListView):
    template_name = "sso/tag_list.html"
    context_object_name = "tags"

    def get_queryset(self):
        from django.contrib.auth.models import Group

        return Group.objects.annotate(member_count=Count("user")).order_by("name")


class TagCreateView(AdminOnlyMixin, View):
    """POST-only: create a Django auth.Group with a slug-safe name.

    Spec §12 open question default: enforce slug format so the synthesised
    'tag:<name>' string in OIDC tokens stays predictable (no spaces, no
    case sensitivity surprises).
    """

    def post(self, request):
        from django.contrib.auth.models import Group

        name = (request.POST.get("name") or "").strip()
        if not _TAG_NAME_RE.match(name):
            return HttpResponseBadRequest(
                "Tag name must match [a-z0-9-]+",
            )
        # Explicit length pre-check so an over-long but otherwise
        # slug-safe name returns a clean 400 instead of bubbling a
        # ``DataError`` from the DB on insert (Group.name is
        # max_length=150 per Django).
        max_length = Group._meta.get_field("name").max_length
        if len(name) > max_length:
            return HttpResponseBadRequest(
                f"Tag name must be at most {max_length} characters.",
            )
        Group.objects.get_or_create(name=name)
        return HttpResponseRedirect(reverse("sso:tag_list"))


class TagDetailView(AdminOnlyMixin, DetailView):
    template_name = "sso/tag_detail.html"
    context_object_name = "tag"

    def get_queryset(self):
        from django.contrib.auth.models import Group

        return Group.objects.all()

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["members"] = self.object.user_set.order_by("username")
        ctx["non_members"] = User.objects.exclude(
            pk__in=self.object.user_set.values_list("pk"),
        ).order_by("username")
        return ctx


class TagMembershipToggleView(AdminOnlyMixin, View):
    """POST-only: toggle a user's membership in a tag (Django auth.Group)."""

    def post(self, request, user_id, group_id):
        from django.contrib.auth.models import Group

        target = get_object_or_404(User, pk=user_id)
        group = get_object_or_404(Group, pk=group_id)

        if target.groups.filter(pk=group.pk).exists():
            target.groups.remove(group)
            verb = "removed"
        else:
            target.groups.add(group)
            verb = "added"

        SsoAuditLog.log(
            event_type=SsoAuditLog.EventType.GROUP_MEMBERSHIP_CHANGED,
            actor=request.user,
            target_user=target,
            message=f"{verb}: {target.username} -> {group.name}",
            ip_address=_client_ip(request),
        )

        if getattr(request, "htmx", False):
            # HTMX caller: re-render the tags card with refreshed entries.
            # This works for both call sites — tag_detail page and user_form
            # (the partial #tags-card root id matches both contexts).
            member_ids = set(target.groups.values_list("pk", flat=True))
            tag_entries = [
                {"group": g, "is_member": g.pk in member_ids}
                for g in Group.objects.order_by("name")
            ]
            return render(
                request,
                "sso/_tags_card.html",
                {
                    "target_user": target,
                    "tag_entries": tag_entries,
                },
            )
        return HttpResponseRedirect(
            reverse("sso:tag_detail", kwargs={"pk": group.pk}),
        )
