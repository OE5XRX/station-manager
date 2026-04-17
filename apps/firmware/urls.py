from django.urls import path

from apps.firmware.views import (
    FirmwareCreateView,
    FirmwareDeleteView,
    FirmwareDetailView,
    FirmwareDownloadView,
    FirmwareListView,
    FirmwareUpdateView,
)

app_name = "firmware"

urlpatterns = [
    path("", FirmwareListView.as_view(), name="firmware_list"),
    path("<int:pk>/", FirmwareDetailView.as_view(), name="firmware_detail"),
    path("upload/", FirmwareCreateView.as_view(), name="firmware_upload"),
    path("<int:pk>/edit/", FirmwareUpdateView.as_view(), name="firmware_edit"),
    path("<int:pk>/delete/", FirmwareDeleteView.as_view(), name="firmware_delete"),
    path("<int:pk>/download/", FirmwareDownloadView.as_view(), name="firmware_download"),
]
