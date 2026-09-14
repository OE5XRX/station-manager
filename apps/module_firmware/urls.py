from django.urls import path

from . import views

app_name = "module_firmware"

urlpatterns = [
    path("", views.ModuleListView.as_view(), name="module_list"),
    path("firmware/", views.FirmwareReleaseListView.as_view(), name="release_list"),
    path("firmware/import/", views.FirmwareImportView.as_view(), name="import"),
    path("firmware/github/", views.GithubFirmwareReleasesPartialView.as_view(), name="gh_partial"),
    path("firmware/<int:pk>/archive/", views.FirmwareArchiveView.as_view(), name="archive"),
    path("firmware/<int:pk>/restore/", views.FirmwareRestoreView.as_view(), name="restore"),
    path("<str:uid>/", views.ModuleDetailView.as_view(), name="module_detail"),
    path("<str:uid>/confirm/", views.ModuleConfirmView.as_view(), name="module_confirm"),
    path("<str:uid>/lifecycle/", views.ModuleLifecycleView.as_view(), name="module_lifecycle"),
    path("<str:uid>/notes/", views.ModuleNotesView.as_view(), name="module_notes"),
]
