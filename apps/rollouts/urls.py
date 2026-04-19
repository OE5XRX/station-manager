from django.urls import path

from . import views

app_name = "rollouts"
urlpatterns = [
    path(
        "upgrade/",
        views.UpgradeDashboardView.as_view(),
        name="upgrade_dashboard",
    ),
    path(
        "upgrade/station/<int:station_pk>/",
        views.UpgradeStationView.as_view(),
        name="upgrade_station",
    ),
    path(
        "upgrade/group/<str:tag_name>/",
        views.UpgradeGroupView.as_view(),
        name="upgrade_group",
    ),
    path("sequence/", views.SequenceEditView.as_view(), name="sequence_edit"),
    path("sequence/add/", views.SequenceAddView.as_view(), name="sequence_add"),
    path(
        "sequence/remove/<int:entry_pk>/",
        views.SequenceRemoveView.as_view(),
        name="sequence_remove",
    ),
    path("sequence/reorder/", views.SequenceReorderView.as_view(), name="sequence_reorder"),
]
