# apps/audio/routing.py
from django.urls import re_path

from . import consumers

# Browser-side (Django session auth via AllowedHostsOriginValidator stack).
websocket_urlpatterns = [
    re_path(r"ws/audio/(?P<station_id>\d+)/$", consumers.AudioConsumer.as_asgi()),
]

# Agent-side (Ed25519 query-param auth; skips origin validation).
agent_websocket_urlpatterns = [
    re_path(
        r"ws/agent/audio/(?P<station_id>\d+)/$",
        consumers.AgentAudioConsumer.as_asgi(),
    ),
]
