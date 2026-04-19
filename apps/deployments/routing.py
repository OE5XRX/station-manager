from django.urls import re_path

from . import consumers

websocket_urlpatterns = [
    re_path(r"ws/deployments/$", consumers.DeploymentStatusConsumer.as_asgi()),
    re_path(r"ws/deployments/status/$", consumers.DeploymentStatusConsumer.as_asgi()),
]
