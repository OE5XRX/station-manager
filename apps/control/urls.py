from django.urls import path

from . import views

app_name = "control"
urlpatterns = [
    path("<int:pk>/control/", views.StationControlView.as_view(), name="station_control"),
]
