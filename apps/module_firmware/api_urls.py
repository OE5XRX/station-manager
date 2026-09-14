from django.urls import path

from . import api_views

app_name = "module_firmware_api"

urlpatterns = [
    path("<int:pk>/download/", api_views.ModuleFirmwareDownloadView.as_view(), name="download"),
]
