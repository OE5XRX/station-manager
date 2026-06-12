from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import CreateView, DeleteView, DetailView, ListView, UpdateView

from .forms import LoginForm, ProfileForm, UserChangeForm, UserCreationForm

User = get_user_model()


class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Mixin that restricts access to users with admin role."""

    def test_func(self):
        return self.request.user.is_admin


class LoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = LoginForm


class LogoutView(auth_views.LogoutView):
    pass


class ProfileView(LoginRequiredMixin, UpdateView):
    template_name = "accounts/profile.html"
    form_class = ProfileForm
    success_url = reverse_lazy("accounts:profile")

    def get_object(self, queryset=None):
        return self.request.user

    def form_valid(self, form):
        messages.success(self.request, _("Profile updated successfully."))
        return super().form_valid(form)


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
        messages.success(self.request, _("User created successfully."))
        return super().form_valid(form)

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
        messages.success(self.request, _("User updated successfully."))
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        return context


class UserDeleteView(AdminRequiredMixin, DeleteView):
    model = User
    template_name = "accounts/user_confirm_delete.html"
    success_url = reverse_lazy("accounts:user_list")
    context_object_name = "target_user"

    def form_valid(self, form):
        if self.get_object() == self.request.user:
            messages.error(self.request, _("You cannot delete your own account."))
            return redirect(self.success_url)
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
