from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
)

from apps.stations.models import StationAssignment

from .forms import (
    LoginForm,
    PasswordChangeForm,
    ProfileAddressForm,
    ProfileIdentityForm,
    ProfileProfileForm,
    UserChangeForm,
    UserCreationForm,
)
from .geocoding import geocode_address, lat_lon_to_locator
from .models import AccountAuditLog

User = get_user_model()


def _client_ip(request):
    """Lazy wrapper around `apps.accounts.views_membership._get_client_ip`.

    Imported lazily to avoid a circular import — views_membership pulls
    `AdminRequiredMixin` from this module at import time.
    """
    from .views_membership import _get_client_ip

    return _get_client_ip(request)


# Set of User fields whose changes are tracked in USER_UPDATED audit
# entries (form_valid diffs form.changed_data against this set). Geocoding-
# derived fields (latitude/longitude) are intentionally NOT tracked — they
# are recomputed from `address`, not user-edited.
TRACKED_USER_FIELDS = frozenset(
    {
        "username",
        "email",
        "first_name",
        "last_name",
        "language",
        "bio",
        "avatar",
        "qth_name",
        "qrz_url",
        "phone",
        "address",
        "locator",
        "is_directory_visible",
    }
)


class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Mixin that restricts access to users with admin role."""

    def test_func(self):
        return self.request.user.is_admin


# The login & logout pages must be reachable without a session, otherwise
# we trap unauthenticated users in a redirect loop the moment
# ``LoginRequiredMiddleware`` is active. ``@login_not_required`` is the
# decorator equivalent of marking the view function for the middleware
# to skip.
@method_decorator(login_not_required, name="dispatch")
class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm


@method_decorator(login_not_required, name="dispatch")
class LogoutView(auth_views.LogoutView):
    pass


class ProfileView(LoginRequiredMixin, TemplateView):
    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.request.user
        ctx["identity_form"] = ProfileIdentityForm(instance=user, prefix="identity")
        ctx["profile_form"] = ProfileProfileForm(instance=user, prefix="profile")
        ctx["address_form"] = ProfileAddressForm(instance=user, prefix="address")
        ctx["password_form"] = PasswordChangeForm(user=user)
        ctx["onboarding_hints"] = self._onboarding_hints(user)
        from apps.sso.views import _active_sessions_for

        ctx["self_sessions"] = _active_sessions_for(user)
        return ctx

    def post(self, request, *args, **kwargs):
        form_name = request.POST.get("form_name", "")
        user = request.user
        if form_name == "identity":
            return self._save_identity(request, user)
        if form_name == "profile":
            return self._save_profile(request, user)
        if form_name == "address":
            return self._save_address(request, user)
        messages.error(request, _("Unknown form."))
        return redirect("accounts:profile")

    def _save_identity(self, request, user):
        form = ProfileIdentityForm(request.POST, instance=user, prefix="identity")
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Identity updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")

    def _save_profile(self, request, user):
        form = ProfileProfileForm(request.POST, request.FILES, instance=user, prefix="profile")
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Profile updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")

    def _save_address(self, request, user):
        # Snapshot the locator from the in-memory user instance BEFORE the
        # form runs is_valid() (which mutates `user` via _post_clean →
        # construct_instance), so _maybe_geocode can restore it on
        # geocode-fail. The form includes locator and would otherwise blow
        # away a previously-stored value when the user edits address
        # without touching locator (POST sends "").
        pre_locator = user.locator
        form = ProfileAddressForm(request.POST, instance=user, prefix="address")
        if form.is_valid():
            changed = set(form.changed_data)
            form.save()
            self._maybe_geocode(user, changed, pre_locator)
            self._emit_user_updated(request, user, changed)
            messages.success(request, _("Address updated."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")

    def _maybe_geocode(self, user, changed_fields, pre_locator=""):
        if "address" not in changed_fields:
            return
        if not user.address:
            user.latitude = None
            user.longitude = None
            if "locator" not in changed_fields:
                user.locator = ""
            user.save(update_fields=["latitude", "longitude", "locator"])
            return
        coords = geocode_address(user.address)
        if coords:
            lat, lon = coords
            user.latitude = lat
            user.longitude = lon
            if "locator" not in changed_fields:
                user.locator = lat_lon_to_locator(float(lat), float(lon))
            user.save(update_fields=["latitude", "longitude", "locator"])
        elif "locator" not in changed_fields and user.locator != pre_locator:
            # Fail closed: Nominatim returned no coords. The browser-rendered
            # form pre-populates the locator input from the instance, so a
            # POST that leaves locator untouched arrives with the existing
            # value and `changed_fields` does not include "locator". If for
            # any reason form.save() still blanked it (e.g. an artificial
            # POST without the field), restore the pre-save value so the
            # user's existing locator isn't lost just because Nominatim was
            # down.
            #
            # Manual-override path: when the user deliberately typed a value
            # (or cleared it), "locator" IS in changed_fields → skip restore
            # and honor the user's intent.
            user.locator = pre_locator
            user.save(update_fields=["locator"])

    def _emit_user_updated(self, request, user, changed_fields):
        tracked = changed_fields & TRACKED_USER_FIELDS
        if not tracked:
            return
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_UPDATED,
            actor=user,
            target_user=user,
            message=f"self-edit changed: {', '.join(sorted(tracked))}",
            ip_address=_client_ip(request),
        )

    def _onboarding_hints(self, user):
        return {
            "name_missing": not (user.first_name or user.last_name),
            "avatar_missing": not user.avatar,
            "bio_missing": not user.bio,
            "qth_missing": not user.qth_name,
            "address_missing": not user.address,
        }


class ProfilePasswordChangeView(LoginRequiredMixin, View):
    """Self-only password change endpoint posted from the Profile page."""

    http_method_names = ["post"]

    def post(self, request):
        from django.contrib.auth import update_session_auth_hash

        form = PasswordChangeForm(user=request.user, data=request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.PASSWORD_CHANGED,
                actor=request.user,
                target_user=request.user,
                message="self-edit changed: password",
                ip_address=_client_ip(request),
            )
            messages.success(request, _("Password updated successfully."))
        else:
            for errors in form.errors.values():
                messages.error(request, "; ".join(errors))
        return redirect("accounts:profile")


class UserListView(LoginRequiredMixin, ListView):
    """Audience-aware list. Admin sees everyone (incl. Applicants),
    Member sees everyone except Applicants, Applicants get 404.
    Filter-bar params (q, role, status) are applied on the audience-filtered
    queryset.
    """

    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25

    def dispatch(self, request, *args, **kwargs):
        from .visibility import user_can_view_directory

        # Let LoginRequiredMixin handle anonymous (redirect to login)
        # before evaluating the directory gate.
        if not request.user.is_authenticated:
            return super().dispatch(request, *args, **kwargs)
        if not user_can_view_directory(request.user):
            raise Http404()
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = User.objects.order_by("username")
        if not self.request.user.is_admin:
            qs = qs.exclude(membership_level=User.MembershipLevel.APPLICANT)

        q = self.request.GET.get("q", "").strip()
        if q:
            qs = qs.filter(
                Q(username__icontains=q)
                | Q(email__icontains=q)
                | Q(first_name__icontains=q)
                | Q(last_name__icontains=q)
            )

        role = self.request.GET.get("role", "")
        valid_roles = {x.value for x in User.MembershipLevel}
        if not self.request.user.is_admin:
            valid_roles -= {User.MembershipLevel.APPLICANT.value}
        if role in valid_roles:
            qs = qs.filter(membership_level=role)

        if self.request.user.is_admin:
            status = self.request.GET.get("status", "")
            if status == "active":
                qs = qs.filter(is_active=True)
            elif status == "inactive":
                qs = qs.filter(is_active=False)

        return qs.prefetch_related(
            "region_assignments__region",
            "station_assignments__station",
        )

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["is_admin_view"] = self.request.user.is_admin
        ctx["is_member_view"] = not self.request.user.is_admin
        ctx["filter_q"] = self.request.GET.get("q", "")
        ctx["filter_role"] = self.request.GET.get("role", "")
        ctx["filter_status"] = self.request.GET.get("status", "")
        return ctx


class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserCreationForm

    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        from django.db import transaction

        from .emails import send_account_email
        from .models import AccountToken
        from .tokens import issue_token

        with transaction.atomic():
            user = form.save(commit=False)
            user.save()
            self.object = user
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_CREATED,
                actor=self.request.user,
                target_user=user,
                message=f"{user.username} <{user.email}>",
                ip_address=_client_ip(self.request),
            )
            raw = issue_token(
                user,
                AccountToken.TokenType.WELCOME,
                ip=_client_ip(self.request),
            )
            send_account_email(
                user,
                "welcome",
                {
                    "raw_token": raw,
                    "actor": self.request.user.username,
                },
            )
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.WELCOME_TOKEN_SENT,
                actor=self.request.user,
                target_user=user,
                message=f"to {user.email}",
                ip_address=_client_ip(self.request),
            )
        messages.success(
            self.request,
            _("User created. Welcome link sent to %(email)s.") % {"email": user.email},
        )
        return redirect(self.get_success_url())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Create User")
        return context


class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserChangeForm

    def get_success_url(self):
        return reverse("accounts:user_detail", kwargs={"pk": self.object.pk})

    def form_valid(self, form):
        changed_fields = set(form.changed_data)
        response = super().form_valid(form)
        self._maybe_geocode(self.object, changed_fields)

        tracked = changed_fields & TRACKED_USER_FIELDS
        if tracked:
            AccountAuditLog.log(
                event_type=AccountAuditLog.EventType.USER_UPDATED,
                actor=self.request.user,
                target_user=self.object,
                message=f"changed: {', '.join(sorted(tracked))}",
                ip_address=_client_ip(self.request),
            )
        if "is_active" in changed_fields:
            event = (
                AccountAuditLog.EventType.USER_ACTIVATED
                if self.object.is_active
                else AccountAuditLog.EventType.USER_DEACTIVATED
            )
            AccountAuditLog.log(
                event_type=event,
                actor=self.request.user,
                target_user=self.object,
                message="",
                ip_address=_client_ip(self.request),
            )

        messages.success(self.request, _("User updated successfully."))
        return response

    def _maybe_geocode(self, user, changed_fields):
        if "address" not in changed_fields:
            return
        if not user.address:
            user.latitude = None
            user.longitude = None
            if "locator" not in changed_fields:
                user.locator = ""
            user.save(update_fields=["latitude", "longitude", "locator"])
            return
        coords = geocode_address(user.address)
        if coords:
            lat, lon = coords
            user.latitude = lat
            user.longitude = lon
            if "locator" not in changed_fields:
                user.locator = lat_lon_to_locator(float(lat), float(lon))
            user.save(update_fields=["latitude", "longitude", "locator"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        return context


class UserDeleteView(AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("accounts:user_list")
    context_object_name = "target_user"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        user = self.object
        ctx["n_station_assignments"] = user.station_assignments.count()
        ctx["n_region_assignments"] = user.region_assignments.count()
        ctx["station_admin_assignments"] = list(
            user.station_assignments.filter(role=StationAssignment.Role.ADMIN).select_related(
                "station"
            )
        )
        ctx["n_sso_grants"] = user.app_grants.count() if hasattr(user, "app_grants") else 0
        ctx["n_active_sessions"] = (
            user.token_sessions.filter(revoked_at__isnull=True).count()
            if hasattr(user, "token_sessions")
            else 0
        )
        ctx["n_group_memberships"] = user.groups.count()
        return ctx

    def form_valid(self, form):
        if self.object == self.request.user:
            messages.error(self.request, _("You cannot delete your own account."))
            return redirect(self.success_url)
        AccountAuditLog.log(
            event_type=AccountAuditLog.EventType.USER_DELETED,
            actor=self.request.user,
            target_user=self.object,
            message=f"{self.object.username} <{self.object.email}>",
            ip_address=_client_ip(self.request),
        )
        messages.success(self.request, _("User deleted successfully."))
        return super().form_valid(form)


class UserDetailView(LoginRequiredMixin, DetailView):
    """Audience-aware detail page.

    Permission flows entirely through ``apps.accounts.visibility``:
      - Admin sees any user (incl. Applicants).
      - Self/Applicant sees own detail.
      - Member sees other Members (not Applicants), reduced fields when
        the target has ``is_directory_visible=False``.
      - Everyone else gets 404 (no existence-leak).
    """

    model = User
    template_name = "accounts/user_detail.html"
    context_object_name = "object"

    def get_object(self, queryset=None):
        obj = super().get_object(queryset)
        from .visibility import audience_for

        aud = audience_for(self.request.user, obj)
        if aud is None:
            raise Http404("User not found")
        self._audience = aud
        return obj

    def get_context_data(self, **kwargs):
        from .visibility import Audience, directory_visible_fields

        ctx = super().get_context_data(**kwargs)
        aud = self._audience
        ctx["audience"] = aud.value
        ctx["is_admin_view"] = aud == Audience.ADMIN
        ctx["is_self_view"] = aud in (Audience.SELF, Audience.APPLICANT)
        ctx["is_member_view"] = aud == Audience.MEMBER
        ctx["visible_fields"] = directory_visible_fields(self.request.user, self.object)

        if aud == Audience.ADMIN:
            ctx.update(self._admin_context_data())
        elif aud in (Audience.SELF, Audience.APPLICANT):
            ctx.update(self._self_context_data())

        # Assignment-Pills für Topology-Tab (alle Audiences, sofern Felder visible).
        # The Admin context already populates `existing_*_assignments` (used by
        # the management cards). For Self/Member the same list also feeds the
        # readonly-mode cards in the Topology tab — without it the cards
        # render their empty-state ("No assignments yet") even when
        # assignments exist. Reuse Admin's already-loaded list when present,
        # otherwise query once and share it with both consumers.
        if "region_assignments" in ctx["visible_fields"]:
            region_pills = ctx.get("existing_region_assignments")
            if region_pills is None:
                region_pills = list(self.object.region_assignments.select_related("region"))
                ctx["existing_region_assignments"] = region_pills
            ctx["region_assignment_pills"] = region_pills
        if "station_assignments" in ctx["visible_fields"]:
            station_pills = ctx.get("existing_station_assignments")
            if station_pills is None:
                station_pills = list(self.object.station_assignments.select_related("station"))
                ctx["existing_station_assignments"] = station_pills
            ctx["station_assignment_pills"] = station_pills

        # Audit-Tab nur für Self + Admin.
        if aud in (Audience.ADMIN, Audience.SELF, Audience.APPLICANT):
            ctx["user_audit_entries"] = self._build_user_audit(self.object)

        return ctx

    def _admin_context_data(self):
        """Admin sees the full management context — equivalent of the
        old UserUpdateView.get_context_data (which itself moves to a
        slim form-only context in a later task).
        """
        from django.contrib.auth.models import Group

        from apps.sso.views import _active_sessions_for, _build_grants_for_user
        from apps.stations.models import Region, Station

        ctx = {
            "app_grants_list": _build_grants_for_user(self.object),
            "user_sessions": _active_sessions_for(self.object),
            "membership_level_choices": User.MembershipLevel.choices,
        }
        member_ids = set(self.object.groups.values_list("pk", flat=True))
        ctx["tag_entries"] = [
            {"group": g, "is_member": g.pk in member_ids} for g in Group.objects.order_by("name")
        ]

        existing_ra = list(self.object.region_assignments.select_related("region"))
        ctx["existing_region_assignments"] = existing_ra
        assigned_region_ids = {ra.region_id for ra in existing_ra}
        ctx["available_regions"] = Region.objects.exclude(pk__in=assigned_region_ids).order_by(
            "name"
        )
        ctx["existing_station_assignments"] = list(
            self.object.station_assignments.select_related("station")
        )
        ctx["all_stations"] = Station.objects.order_by("name")
        return ctx

    def _self_context_data(self):
        """Self only needs own SSO sessions (so the template can show
        the Self-Sessions card with revoke-own).
        """
        from apps.sso.views import _active_sessions_for

        return {"user_sessions": _active_sessions_for(self.object)}

    def _build_user_audit(self, target_user):
        """Merge AccountAuditLog (target_user=...) + SsoAuditLog
        (target_user OR actor matches) into a (category, entry)
        list sorted by created_at desc, capped at the top 50.
        """
        from django.db.models import Q

        from apps.accounts.models import AccountAuditLog
        from apps.sso.models import SsoAuditLog

        MAX_PER_SOURCE = 500  # noqa: N806 — intentional UPPER_CASE inline constant

        account_qs = (
            AccountAuditLog.objects.filter(target_user=target_user)
            .select_related("actor", "region")
            .order_by("-created_at")[:MAX_PER_SOURCE]
        )
        sso_qs = (
            SsoAuditLog.objects.filter(Q(target_user=target_user) | Q(actor=target_user))
            .select_related("actor", "target_user", "application")
            .order_by("-created_at")[:MAX_PER_SOURCE]
        )
        merged = [("account", e) for e in account_qs] + [("sso", e) for e in sso_qs]
        merged.sort(key=lambda pair: pair[1].created_at, reverse=True)
        return merged[:50]
