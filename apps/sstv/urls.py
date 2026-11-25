from django.urls import path

from . import views, views_gallery

app_name = "sstv"
urlpatterns = [
    path("<int:pk>/sstv/gallery/", views_gallery.SstvGalleryView.as_view(), name="gallery"),
    path(
        "<int:pk>/sstv/<uuid:image_id>/image/",
        views_gallery.SstvPictureView.as_view(),
        name="image",
    ),
    path(
        "<int:pk>/sstv/<uuid:image_id>/download/",
        views_gallery.SstvDownloadView.as_view(),
        name="download",
    ),
    path(
        "<int:pk>/sstv/<uuid:image_id>/delete/",
        views_gallery.SstvDeleteView.as_view(),
        name="delete",
    ),
    path("<int:pk>/sstv/preview/", views.PreviewView.as_view(), name="preview"),
    path(
        "<int:pk>/sstv/preview/<uuid:tx_id>/",
        views.PreviewImageView.as_view(),
        name="preview_image",
    ),
]
