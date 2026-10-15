"""WebSocket route compositions (side-effect free; shared by asgi.py and guard tests)."""

from apps.audio import routing as audio_routing
from apps.control import routing as control_routing
from apps.deployments import routing as deployments_routing
from apps.stations import routing as stations_routing
from apps.tunnel import routing as tunnel_routing

ROUTING_MODULES = (
    audio_routing,
    control_routing,
    deployments_routing,
    stations_routing,
    tunnel_routing,
)

browser_ws_routes = (
    stations_routing.websocket_urlpatterns
    + deployments_routing.websocket_urlpatterns
    + tunnel_routing.websocket_urlpatterns
    + control_routing.websocket_urlpatterns
    + audio_routing.websocket_urlpatterns
)

# Agent routes authenticate themselves via Ed25519 signature in query
# params — they skip AllowedHostsOriginValidator because the station agent
# is a CLI client that doesn't send an Origin header.
agent_ws_routes = (
    tunnel_routing.agent_websocket_urlpatterns
    + control_routing.agent_websocket_urlpatterns
    + audio_routing.agent_websocket_urlpatterns
)
