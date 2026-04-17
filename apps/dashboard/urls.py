from django.urls import path

from apps.dashboard.views import IndexView

app_name = "dashboard"

urlpatterns = [
    path("", IndexView.as_view(), name="index"),
]
