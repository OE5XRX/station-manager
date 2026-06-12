from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth import views as auth_views
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.http import Http404
from django.shortcuts import redirect
from django.urls import reverse_lazy
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


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    paginate_by = 25
    queryset = User.objects.order_by("username")


class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    template_name = "accounts/user_form.html"
    form_class = UserCreationForm
    success_url = reverse_lazy("accounts:user_list")

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
    success_url = reverse_lazy("accounts:user_list")

    def form_valid(self, form):
        messages.success(self.request, _("User updated successfully."))
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_title"] = _("Edit User")
        # Local import: avoids loading apps.sso at module-load time
        # (defensive against import-cycle surprises).
        from django.contrib.auth.models import Group

        from apps.sso.views import _active_sessions_for, _build_grants_for_user

        context["app_grants_list"] = _build_grants_for_user(self.object)
        context["user_sessions"] = _active_sessions_for(self.object)
        # Tag-membership picker: every defined Group with current membership flag.
        member_ids = set(self.object.groups.values_list("pk", flat=True))
        context["tag_entries"] = [
            {"group": g, "is_member": g.pk in member_ids} for g in Group.objects.order_by("name")
        ]
        # Membership-level picker uses the model's TextChoices.
        context["membership_level_choices"] = User.MembershipLevel.choices

        # Region-Assignment card.
        from apps.stations.models import Region, Station

        existing_ra = list(self.object.region_assignments.select_related("region"))
        context["existing_region_assignments"] = existing_ra
        assigned_region_ids = {ra.region_id for ra in existing_ra}
        context["available_regions"] = Region.objects.exclude(pk__in=assigned_region_ids).order_by(
            "name"
        )

        # Station-Assignment card.
        context["existing_station_assignments"] = list(
            self.object.station_assignments.select_related("station")
        )
        context["all_stations"] = Station.objects.order_by("name")
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
        return ctx
