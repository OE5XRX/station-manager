import asyncio
import json

import pytest
from asgiref.sync import sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.testing import WebsocketCommunicator
from django.urls import path
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIClient
from rest_framework.viewsets import ViewSet

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.permissions import AreaConsumerMixin, AreaPermission


class ImgViewSet(ViewSet):
    permission_classes = [AreaPermission]
    area = "images"
    verb = None
    verbs = None

    def list(self, request):
        return Response([])

    @action(detail=False, methods=["get"], verb="manage")
    def available(self, request):
        return Response([])


urlpatterns = [
    path("t/img/", ImgViewSet.as_view({"get": "list"})),
    path("t/img/available/", ImgViewSet.as_view({"get": "available"}, verb="manage")),
]

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.urls(__name__)]


def _u(level):
    return User.objects.create_user(username=f"a-{level}", password="x", membership_level=level)


def test_api_view_allowed_for_applicant():
    c = APIClient()
    c.force_authenticate(_u("applicant"))
    assert c.get("/t/img/").status_code == 200


def test_api_action_verb_denied_with_body():
    c = APIClient()
    c.force_authenticate(_u("staff"))
    r = c.get("/t/img/available/")
    assert r.status_code == 403
    assert r.json()["required"] == "images.manage"
    assert r.json()["your_level"] == "staff"
    assert AccountAuditLog.objects.get(event_type="permission_denied").details["channel"] == "api"


def test_api_anonymous_not_logged():
    r = APIClient().get("/t/img/")
    assert r.status_code in (401, 403)
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


class DummyConsumer(AreaConsumerMixin, AsyncWebsocketConsumer):
    area = "control"

    async def connect(self):
        await self.accept()
        await self.area_connect_gate()

    async def receive(self, text_data=None, bytes_data=None):
        if await self.area_allows("use", request_id="r1"):
            await self.send(text_data=json.dumps({"type": "ok"}))


async def _connect(user):
    comm = WebsocketCommunicator(DummyConsumer.as_asgi(), "/ws/t/")
    comm.scope["user"] = user
    comm.scope["client"] = ("10.1.1.1", 1234)
    ok, _ = await comm.connect()
    return comm


def test_ws_connect_denied_frame_and_4403():
    user = _u("applicant")
    User.objects.filter(pk=user.pk).update(is_active=False)

    async def scenario():
        comm = await _connect(user)
        frame = json.loads(await comm.receive_from())
        assert frame["type"] == "error" and frame["code"] == "forbidden"
        assert (await comm.receive_output())["code"] == 4403

    asyncio.run(scenario())


def test_ws_action_uses_fresh_user():
    user = _u("member")

    async def scenario():
        comm = await _connect(user)
        await comm.send_to(text_data="{}")
        assert json.loads(await comm.receive_from())["type"] == "ok"
        await sync_to_async(User.objects.filter(pk=user.pk).update)(membership_level="applicant")
        await comm.send_to(text_data="{}")
        frame = json.loads(await comm.receive_from())
        assert frame == {
            "type": "error",
            "code": "forbidden",
            "message": frame["message"],
            "request_id": "r1",
        }
        await comm.disconnect()

    asyncio.run(scenario())
