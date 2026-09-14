from django.urls import path

from . import views

app_name = "module_firmware"

urlpatterns = [
    path("", views.ModuleListView.as_view(), name="module_list"),
    path("<str:uid>/", views.ModuleDetailView.as_view(), name="module_detail"),
    path("<str:uid>/confirm/", views.ModuleConfirmView.as_view(), name="module_confirm"),
    path("<str:uid>/lifecycle/", views.ModuleLifecycleView.as_view(), name="module_lifecycle"),
    path("<str:uid>/notes/", views.ModuleNotesView.as_view(), name="module_notes"),
]
