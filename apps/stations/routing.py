from django.urls import re_path

from apps.stations import consumers

websocket_urlpatterns = [
    re_path(r"ws/stations/status/$", consumers.StationStatusConsumer.as_asgi()),
]
