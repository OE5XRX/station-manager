from django.urls import re_path

from . import consumers

# Browser-side WebSocket routes (rely on AllowedHostsOriginValidator + Django
# session auth).
websocket_urlpatterns = [
    re_path(r"ws/terminal/(?P<station_id>\d+)/$", consumers.TerminalConsumer.as_asgi()),
]

# Agent-side WebSocket routes (authenticate via Ed25519 signature in query
# params — they don't go through AllowedHostsOriginValidator because the
# station agent is a CLI client without an Origin header).
agent_websocket_urlpatterns = [
    re_path(
        r"ws/agent/terminal/(?P<station_id>\d+)/$",
        consumers.AgentTerminalConsumer.as_asgi(),
    ),
]
