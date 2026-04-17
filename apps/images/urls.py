from django.urls import path

from . import views

app_name = "images"

urlpatterns = [
    path("", views.ImageListView.as_view(), name="list"),
    path("import/", views.ImageImportView.as_view(), name="import"),
    path("<int:pk>/mark-latest/", views.ImageMarkLatestView.as_view(), name="mark_latest"),
    path("<int:pk>/delete/", views.ImageDeleteView.as_view(), name="delete"),
]
