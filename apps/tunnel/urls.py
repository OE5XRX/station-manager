from django.urls import path

from . import views

app_name = "tunnel"

urlpatterns = [
    path("status/<int:station_id>/", views.TerminalStatusView.as_view(), name="status"),
]
