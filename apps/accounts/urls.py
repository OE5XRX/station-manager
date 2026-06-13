from django.contrib.auth.decorators import login_not_required
from django.urls import path
from django.utils.decorators import method_decorator
from django.views.generic import RedirectView

from . import views
from .views_membership import MembershipSetView
from .views_region_assignments import (
    RegionAssignmentCreateView,
    RegionAssignmentRevokeView,
)
from .views_station_assignments import (
    StationAssignmentCreateView,
    StationAssignmentRevokeView,
)

# Stubs for the email-helper to reverse() against; Tasks 8 + 13 replace them.
_stub = method_decorator(login_not_required, name="dispatch")(RedirectView)

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path(
        "profile/password/",
        views.ProfilePasswordChangeView.as_view(),
        name="password_change",
    ),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
    path("users/<int:pk>/", views.UserDetailView.as_view(), name="user_detail"),
    path("users/<int:pk>/edit/", views.UserUpdateView.as_view(), name="user_edit"),
    path("users/<int:pk>/delete/", views.UserDeleteView.as_view(), name="user_delete"),
    path(
        "users/<int:pk>/membership/",
        MembershipSetView.as_view(),
        name="membership_set",
    ),
    path(
        "users/<int:user_pk>/region_assignments/",
        RegionAssignmentCreateView.as_view(),
        name="region_assignment_create",
    ),
    path(
        "region_assignments/<int:pk>/revoke/",
        RegionAssignmentRevokeView.as_view(),
        name="region_assignment_revoke",
    ),
    path(
        "users/<int:user_pk>/station_assignments/",
        StationAssignmentCreateView.as_view(),
        name="station_assignment_create",
    ),
    path(
        "station_assignments/<int:pk>/revoke/",
        StationAssignmentRevokeView.as_view(),
        name="station_assignment_revoke",
    ),
    path(
        "set-password/<str:token>/",
        _stub.as_view(url="/", permanent=False),
        name="set_password",
    ),
    path(
        "verify-email/<str:token>/",
        _stub.as_view(url="/", permanent=False),
        name="verify_email",
    ),
]
