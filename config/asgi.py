"""ASGI config for station-manager project."""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
django_asgi_app = get_asgi_application()

from apps.deployments import routing as deployments_routing  # noqa: E402
from apps.stations import routing as stations_routing  # noqa: E402
from apps.tunnel import routing as tunnel_routing  # noqa: E402

browser_ws_routes = (
    stations_routing.websocket_urlpatterns
    + deployments_routing.websocket_urlpatterns
    + tunnel_routing.websocket_urlpatterns
)

# Agent routes authenticate themselves via Ed25519 signature in query
# params — they skip AllowedHostsOriginValidator because the station agent
# is a CLI client that doesn't send an Origin header.
agent_ws_routes = tunnel_routing.agent_websocket_urlpatterns


async def websocket_app(scope, receive, send):
    """Dispatch WebSocket requests: agent routes skip origin validation."""
    path = scope.get("path", "")
    if path.startswith("/ws/agent/"):
        inner = URLRouter(agent_ws_routes)
    else:
        inner = AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(browser_ws_routes)))
    return await inner(scope, receive, send)


application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": websocket_app,
    }
)
