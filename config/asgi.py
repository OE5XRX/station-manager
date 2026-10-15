"""ASGI config for station-manager project."""

import os

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from channels.security.websocket import AllowedHostsOriginValidator
from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")
django_asgi_app = get_asgi_application()

from config.ws_routes import agent_ws_routes, browser_ws_routes  # noqa: E402


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
