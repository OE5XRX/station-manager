from django.urls import path

from . import views

app_name = "images"

urlpatterns = [
    path("", views.ImageListView.as_view(), name="list"),
    path("import/", views.ImageImportView.as_view(), name="import"),
]
