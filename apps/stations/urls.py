from django.urls import path

from . import views

app_name = "stations"

urlpatterns = [
    # Stations
    path("", views.StationListView.as_view(), name="station_list"),
    path("create/", views.StationCreateView.as_view(), name="station_create"),
    path("<int:pk>/", views.StationDetailView.as_view(), name="station_detail"),
    path("<int:pk>/edit/", views.StationUpdateView.as_view(), name="station_edit"),
    path("<int:pk>/delete/", views.StationDeleteView.as_view(), name="station_delete"),
    path(
        "<int:pk>/key/generate/",
        views.StationGenerateKeyView.as_view(),
        name="station_generate_key",
    ),
    path(
        "<int:pk>/key/revoke/",
        views.StationRevokeKeyView.as_view(),
        name="station_revoke_key",
    ),
    path(
        "<int:pk>/photos/upload/",
        views.StationPhotoUploadView.as_view(),
        name="station_photo_upload",
    ),
    path(
        "<int:pk>/log/add/",
        views.StationLogEntryCreateView.as_view(),
        name="station_log_add",
    ),
    # Tags
    path("tags/", views.StationTagListView.as_view(), name="tag_list"),
    path("tags/create/", views.StationTagCreateView.as_view(), name="tag_create"),
    path("tags/<int:pk>/edit/", views.StationTagUpdateView.as_view(), name="tag_edit"),
    path("tags/<int:pk>/delete/", views.StationTagDeleteView.as_view(), name="tag_delete"),
]
