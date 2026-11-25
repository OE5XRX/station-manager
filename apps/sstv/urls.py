from django.urls import path

from . import views

app_name = "sstv"
urlpatterns = [
    path("<int:pk>/sstv/preview/", views.PreviewView.as_view(), name="preview"),
    path(
        "<int:pk>/sstv/preview/<uuid:tx_id>/",
        views.PreviewImageView.as_view(),
        name="preview_image",
    ),
]
