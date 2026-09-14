from django.urls import path

from . import api_views

urlpatterns = [
    path("<int:pk>/download/", api_views.ModuleFirmwareDownloadView.as_view(), name="download"),
]
