from django.urls import path

from . import views

app_name = "webpush"

urlpatterns = [
    path("sw.js", views.service_worker, name="service_worker"),
    path("manifest.webmanifest", views.manifest, name="manifest"),
]
