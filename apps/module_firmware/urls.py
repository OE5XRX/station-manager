from django.urls import path

from . import views, views_convergence, views_targets, views_types

app_name = "module_firmware"

urlpatterns = [
    path("", views.ModuleListView.as_view(), name="module_list"),
    path("firmware/", views.FirmwareReleaseListView.as_view(), name="release_list"),
    path("firmware/import/", views.FirmwareImportView.as_view(), name="import"),
    path("firmware/jobs/", views.ImportJobsPartialView.as_view(), name="import_jobs"),
    path("firmware/github/", views.GitHubFirmwareReleasesPartialView.as_view(), name="gh_partial"),
    path("firmware/<int:pk>/archive/", views.FirmwareArchiveView.as_view(), name="archive"),
    path("firmware/<int:pk>/restore/", views.FirmwareRestoreView.as_view(), name="restore"),
    path("types/", views_types.ModuleTypeListView.as_view(), name="type_list"),
    path("types/new/", views_types.ModuleTypeCreateView.as_view(), name="type_create"),
    path("types/<int:pk>/edit/", views_types.ModuleTypeUpdateView.as_view(), name="type_update"),
    path("types/<int:pk>/delete/", views_types.ModuleTypeDeleteView.as_view(), name="type_delete"),
    path("targets/", views_targets.TargetListView.as_view(), name="target_list"),
    path("targets/new/", views_targets.TargetCreateView.as_view(), name="target_create"),
    path("targets/<int:pk>/edit/", views_targets.TargetUpdateView.as_view(), name="target_update"),
    path(
        "targets/<int:pk>/delete/",
        views_targets.TargetDeleteView.as_view(),
        name="target_delete",
    ),
    path(
        "convergence/",
        views_convergence.ConvergenceOverviewView.as_view(),
        name="convergence",
    ),
    path("<str:uid>/", views.ModuleDetailView.as_view(), name="module_detail"),
    path("<str:uid>/confirm/", views.ModuleConfirmView.as_view(), name="module_confirm"),
    path("<str:uid>/lifecycle/", views.ModuleLifecycleView.as_view(), name="module_lifecycle"),
    path("<str:uid>/notes/", views.ModuleNotesView.as_view(), name="module_notes"),
    path(
        "<str:uid>/release-quarantine/",
        views_convergence.ModuleReleaseQuarantineView.as_view(),
        name="module_release_quarantine",
    ),
]
