from django.urls import path

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
    path(
        "users/<int:pk>/soft-delete/",
        views.UserSoftDeleteView.as_view(),
        name="user_soft_delete",
    ),
    path(
        "users/<int:pk>/restore/",
        views.UserRestoreView.as_view(),
        name="user_restore",
    ),
    path(
        "users/<int:pk>/hard-purge/",
        views.UserHardPurgeView.as_view(),
        name="user_hard_purge",
    ),
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
        "users/<int:pk>/welcome/",
        views.ResendWelcomeView.as_view(),
        name="resend_welcome",
    ),
    path(
        "set-password/<str:token>/",
        views.SetPasswordView.as_view(),
        name="set_password",
    ),
    path(
        "password-reset/",
        views.PasswordResetRequestView.as_view(),
        name="password_reset_request",
    ),
    path(
        "verify-email/<str:token>/",
        views.VerifyEmailView.as_view(),
        name="verify_email",
    ),
]
