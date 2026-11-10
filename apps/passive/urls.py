from django.urls import path

from . import views

app_name = "passive"
urlpatterns = [
    path("aprs/", views.AprsPageView.as_view(), name="aprs"),
    path("aprs/api/packets/", views.AprsPacketsView.as_view(), name="aprs_packets"),
    path("aprs/api/igates/", views.AprsIgatesView.as_view(), name="aprs_igates"),
    path("aprs/einstellungen/", views.AprsSettingsView.as_view(), name="aprs_settings"),
]
