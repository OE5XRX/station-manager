from django.urls import path

from . import views

app_name = "repeaters"
urlpatterns = [
    path("", views.RepeaterListView.as_view(), name="list"),
    path("new/", views.RepeaterCreateView.as_view(), name="create"),
    path("<int:pk>/edit/", views.RepeaterUpdateView.as_view(), name="update"),
    path("<int:pk>/deactivate/", views.RepeaterDeactivateView.as_view(), name="deactivate"),
    path("scanlists/", views.ScanListIndexView.as_view(), name="scanlists"),
    path("scanlists/mine.json", views.ScanListMineJsonView.as_view(), name="scanlists_mine"),
    path("scanlists/new/", views.ScanListCreateView.as_view(), name="scanlist_create"),
    path("scanlists/<int:pk>/", views.ScanListDetailView.as_view(), name="scanlist_detail"),
    path("scanlists/<int:pk>/delete/", views.ScanListDeleteView.as_view(), name="scanlist_delete"),
    path(
        "scanlists/<int:pk>/entries/add/",
        views.ScanListEntryAddView.as_view(),
        name="scanlist_entry_add",
    ),
    path(
        "scanlists/<int:pk>/entries/<int:entry_pk>/delete/",
        views.ScanListEntryDeleteView.as_view(),
        name="scanlist_entry_delete",
    ),
    path(
        "scanlists/<int:pk>/entries/<int:entry_pk>/move/",
        views.ScanListEntryMoveView.as_view(),
        name="scanlist_entry_move",
    ),
]
