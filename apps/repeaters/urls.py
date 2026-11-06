from django.urls import path

from . import views

app_name = "repeaters"
urlpatterns = [
    path("", views.RepeaterListView.as_view(), name="list"),
    path("new/", views.RepeaterCreateView.as_view(), name="create"),
    path("<int:pk>/edit/", views.RepeaterUpdateView.as_view(), name="update"),
    path("<int:pk>/deactivate/", views.RepeaterDeactivateView.as_view(), name="deactivate"),
]
