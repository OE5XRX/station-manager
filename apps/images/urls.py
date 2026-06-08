from django.urls import path

from . import views

app_name = "images"

urlpatterns = [
    path("", views.ImageListView.as_view(), name="list"),
    path(
        "github-releases/",
        views.GitHubReleasesPartialView.as_view(),
        name="gh_partial",
    ),
    path(
        "github-releases/queue/",
        views.QuickQueueView.as_view(),
        name="gh_queue",
    ),
    path("<int:pk>/mark-latest/", views.ImageMarkLatestView.as_view(), name="mark_latest"),
    path("<int:pk>/delete/", views.ImageDeleteView.as_view(), name="delete"),
    path("<int:pk>/archive/", views.ImageArchiveView.as_view(), name="archive"),
    path("<int:pk>/restore/", views.ImageRestoreView.as_view(), name="restore"),
]
