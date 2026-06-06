from django.urls import path

from . import views
from .views_membership import MembershipSetView
from .views_region_assignments import (
    RegionAssignmentCreateView,
    RegionAssignmentRevokeView,
)

app_name = "accounts"

urlpatterns = [
    path("login/", views.LoginView.as_view(), name="login"),
    path("logout/", views.LogoutView.as_view(), name="logout"),
    path("profile/", views.ProfileView.as_view(), name="profile"),
    path("users/", views.UserListView.as_view(), name="user_list"),
    path("users/create/", views.UserCreateView.as_view(), name="user_create"),
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
]
