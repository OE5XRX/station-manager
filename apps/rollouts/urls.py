from django.urls import path

from . import views

app_name = "rollouts"
urlpatterns = [
    path(
        "upgrade/",
        views._upgrade_dashboard_placeholder,
        name="upgrade_dashboard",
    ),
    path(
        "upgrade/station/<int:station_pk>/",
        views.UpgradeStationView.as_view(),
        name="upgrade_station",
    ),
    path(
        "upgrade/group/<str:tag_slug>/",
        views.UpgradeGroupView.as_view(),
        name="upgrade_group",
    ),
]
