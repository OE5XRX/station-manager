"""Permission matrix: every level × every declared area/verb, per channel (spec §6.2).

One representative endpoint per (area, verb) and channel — UI (Django view), API
(DRF) and WebSocket (Channels consumer). The expected outcome is always computed
from ``required_level()``; no level is hard-coded here, so changing the registry
(``apps.accounts.permissions.AREAS``) re-targets every assertion.

Outcome semantics:

* UI: anonymous → login redirect; allowed → anything but 403 (business rules may
  still answer 302/400/404); denied → 403.
* API: anonymous → 401/403; allowed → not 403; denied → 403 whose ``required``
  names the area/verb the request resolves to (the table only uses requests whose
  method resolves to the declared verb).
* WS: anonymous → close 4401; connect allowed → no forbidden frame / 4403 close;
  connect denied → forbidden frame + close 4403; actions allowed → no forbidden
  frame for the request; actions denied → forbidden frame for the request.

No pytest-asyncio: WS scenarios run via asyncio.run() in transactional tests.
"""

import asyncio
import json

import pytest
from channels.layers import get_channel_layer
from channels.testing import WebsocketCommunicator
from django.contrib.auth.models import AnonymousUser
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.permissions import AREAS, VERBS, AreaPermission, Level, required_level
from apps.images import github_releases
from apps.stations.models import Station
from config.asgi import application

LEVELS = [None, Level.APPLICANT, Level.MEMBER, Level.STAFF, Level.ADMIN]


def _level_id(level):
    return level.slug if level else "anon"


def _key_id(key):
    return f"{key[0]}.{key[1]}"


# (area, verb) -> (method, url_name, objs-key for the single URL arg or None, POST data)
UI = {
    ("dashboard", "view"): ("GET", "dashboard:index", None, None),
    ("profile", "view"): ("GET", "accounts:profile", None, None),
    ("profile", "use"): ("POST", "accounts:notification_settings", None, {}),
    ("stations", "view"): ("GET", "stations:station_detail", "station", None),
    ("stations", "manage"): ("GET", "stations:station_edit", "station", None),
    ("control", "view"): ("GET", "control:station_control", "station", None),
    ("terminal", "use"): ("GET", "tunnel:status", "station", None),
    ("topology", "view"): ("GET", "stations:region_list", None, None),
    ("topology", "manage"): ("GET", "stations:region_create", None, None),
    ("deployments", "view"): ("GET", "deployments:deployment_list", None, None),
    ("deployments", "manage"): ("GET", "deployments:deployment_create", None, None),
    ("rollouts", "view"): ("GET", "rollouts:upgrade_dashboard", None, None),
    ("rollouts", "manage"): ("GET", "rollouts:sequence_edit", None, None),
    ("images", "view"): ("GET", "images:list", None, None),
    ("images", "manage"): ("POST", "images:archive", "image", {}),
    ("provisioning", "manage"): ("GET", "provisioning:status", "provisioning_job", None),
    ("alerts", "view"): ("GET", "monitoring:alert_list", None, None),
    ("alerts", "manage"): ("POST", "monitoring:alert_acknowledge", "alert", {}),
    ("alert_rules", "view"): ("GET", "monitoring:alert_settings", None, None),
    ("alert_rules", "manage"): ("POST", "monitoring:test_push", None, {}),
    ("modules", "view"): ("GET", "module_firmware:module_list", None, None),
    # Without ?module_type the partial renders its "no module type" state: no GitHub call.
    ("modules", "manage"): ("GET", "module_firmware:gh_partial", None, None),
    ("users", "view"): ("GET", "accounts:user_list", None, None),
    ("users", "manage"): ("GET", "accounts:user_create", None, None),
    ("api_tokens", "use"): ("GET", "accounts:api_tokens", None, None),
    ("audit", "view"): ("GET", "audit:audit_list", None, None),
    ("sso", "manage"): ("GET", "sso:dashboard", None, None),
}
# control.use / control.manage live on the WS channel (WS table below).
UI_NOT_APPLICABLE = {("control", "use"), ("control", "manage")}

# (area, verb) -> (method, url with {objs-key} placeholders, JSON body or None)
API = {
    ("dashboard", "view"): ("GET", "/api/v1/", None),
    ("stations", "view"): ("GET", "/api/v1/stations/", None),
    ("stations", "manage"): ("PATCH", "/api/v1/stations/{station}/", {"name": "m"}),
    ("topology", "view"): ("GET", "/api/v1/regions/", None),
    ("topology", "manage"): ("POST", "/api/v1/regions/", {"name": "mx"}),
    ("deployments", "view"): ("GET", "/api/v1/deployments/", None),
    ("deployments", "manage"): ("POST", "/api/v1/deployments/", {}),
    ("rollouts", "view"): ("GET", "/api/v1/rollout-sequences/", None),
    ("rollouts", "manage"): ("POST", "/api/v1/rollout-sequences/", {}),
    ("images", "view"): ("GET", "/api/v1/images/", None),
    # GET, but the action declares verb="manage" (GitHub call mocked in the test).
    ("images", "manage"): ("GET", "/api/v1/images/available/", None),
    # Empty body: the allowed Admin gets a 400, no job is fired.
    ("provisioning", "manage"): ("POST", "/api/v1/provisioning-jobs/", {}),
    ("alerts", "view"): ("GET", "/api/v1/alerts/", None),
    ("alert_rules", "view"): ("GET", "/api/v1/alert-rules/", None),
    ("alert_rules", "manage"): ("POST", "/api/v1/alert-rules/", {}),
    ("users", "view"): ("GET", "/api/v1/users/", None),
    ("users", "manage"): ("PATCH", "/api/v1/users/{other_user}/", {"first_name": "Z"}),
    ("control", "manage"): ("POST", "/api/v1/stations/{station}/audio-diagnostics/", {}),
}

# (ws path builder, area, verb, action)
WS = [
    ("stations-status", "stations", "view", "connect"),
    ("deployment-status", "deployments", "view", "connect"),
    ("control", "control", "view", "connect"),
    ("control", "control", "use", "lock_acquire"),
    ("control", "control", "manage", "command:filter_hpf"),
    ("audio", "control", "view", "connect"),
    ("audio", "control", "use", "mic_open"),
    ("terminal", "terminal", "use", "connect"),
]
WS_PATHS = {
    "stations-status": lambda sid: "/ws/stations/status/",
    "deployment-status": lambda sid: "/ws/deployments/status/",
    "control": lambda sid: f"/ws/control/{sid}/",
    "audio": lambda sid: f"/ws/audio/{sid}/",
    "terminal": lambda sid: f"/ws/terminal/{sid}/",
}


def _resolvable():
    for area, spec in AREAS.items():
        for verb in VERBS:
            if getattr(spec, verb) is not None:  # declared verbs only
                yield area, verb


def _walk(patterns, prefix=""):
    for p in patterns:
        route = prefix + str(p.pattern)
        if isinstance(p, URLResolver):
            yield from _walk(p.url_patterns, route)
        elif isinstance(p, URLPattern):
            yield route, p.callback


def _api_areas():
    """Areas of every user-facing DRF endpoint under /api/ (AreaPermission-guarded)."""
    areas = set()
    for route, callback in _walk(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if not route.startswith("api/") or cls is None:
            continue
        if AreaPermission in getattr(cls, "permission_classes", ()) and cls.area:
            areas.add(cls.area)
    return areas


def test_matrix_covers_every_declared_verb():
    missing_ui = [k for k in _resolvable() if k not in UI and k not in UI_NOT_APPLICABLE]
    assert not missing_ui, missing_ui
    # Every declared verb is exercised on at least one channel.
    ws_keys = {(area, verb) for _, area, verb, _ in WS}
    untested = [k for k in _resolvable() if k not in UI and k not in ws_keys]
    assert not untested, untested
    # No stale rows after a registry change: every table key resolves to some level
    # (an undeclared verb such as profile.view inherits the next stricter one).
    for area, verb in [*UI, *API, *ws_keys]:
        assert required_level(area, verb) is not None, (area, verb)


def test_matrix_covers_every_api_area():
    api_areas = _api_areas()
    assert api_areas, "URL walk found no API endpoints"
    assert {a for a, _ in API} == api_areas


def _expected_allowed(level, area, verb):
    need = required_level(area, verb)
    return level is not None and need is not None and level >= need


def _user(level):
    if level is None:
        return None
    return User.objects.create_user(
        username=f"mx-{level.slug}", password="x", membership_level=level.slug
    )


@pytest.fixture
def objs(db, station, image_release, alert, deployment, admin_user, monkeypatch):
    from apps.provisioning.models import ProvisioningJob

    # No network: the images.manage API representative proxies GitHub releases.
    monkeypatch.setattr(github_releases, "fetch_releases", lambda *a, **k: [])
    other = User.objects.create_user(username="other", password="x", membership_level="member")
    job = ProvisioningJob.objects.create(
        station=station, image_release=image_release, requested_by=admin_user
    )
    return {
        "station": station.pk,
        "image": image_release.pk,
        "alert": alert.pk,
        "provisioning_job": job.pk,
        "other_user": other.pk,
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS, ids=_level_id)
@pytest.mark.parametrize("key", sorted(UI), ids=_key_id)
def test_ui_matrix(client, objs, level, key):
    method, name, kw, data = UI[key]
    url = reverse(name, args=[objs[kw]] if kw else [])
    user = _user(level)
    if user:
        client.force_login(user)
    resp = client.get(url) if method == "GET" else client.post(url, data or {})
    if level is None:
        assert resp.status_code == 302 and "/login" in resp["Location"], resp.status_code
    elif _expected_allowed(level, *key):
        assert resp.status_code != 403, (key, level, resp.status_code)
    else:
        assert resp.status_code == 403, (key, level, resp.status_code)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS, ids=_level_id)
@pytest.mark.parametrize("key", sorted(API), ids=_key_id)
def test_api_matrix(objs, level, key):
    method, url, data = API[key]
    url = url.format(**objs)
    c = APIClient()
    user = _user(level)
    if user:
        c.force_authenticate(user)
    call = getattr(c, method.lower())
    resp = call(url, data, format="json") if data is not None else call(url)
    if level is None:
        assert resp.status_code in (401, 403), resp.status_code
    elif _expected_allowed(level, *key):
        assert resp.status_code != 403, (key, level, resp.status_code, getattr(resp, "data", None))
    else:
        assert resp.status_code == 403, (key, level, resp.status_code)
        assert resp.json()["required"] == f"{key[0]}.{key[1]}"


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------


async def _outputs(comm, timeout=0.4):
    """Everything the consumer sent until it goes quiet (or closes).

    ``receive_nothing`` is used as the quiet probe because ``receive_output``
    cancels the application on timeout.
    """
    out = []
    while not await comm.receive_nothing(timeout=timeout):
        o = await comm.receive_output()
        out.append(o)
        if o["type"] == "websocket.close":
            break
    return out


def _frames(outputs):
    return [
        json.loads(o["text"]) for o in outputs if o["type"] == "websocket.send" and o.get("text")
    ]


def _closes(outputs):
    return [o.get("code") for o in outputs if o["type"] == "websocket.close"]


def _forbidden(frames, request_id=None):
    return [
        f
        for f in frames
        if f.get("type") == "error"
        and f.get("code") == "forbidden"
        and (request_id is None or f.get("request_id") == request_id)
    ]


def _agent_comm(station_id):
    # ed25519 verification bypassed via the control_agent_auth fixture.
    return WebsocketCommunicator(
        application, f"/ws/agent/control/{station_id}/?signature=x&timestamp=0"
    )


async def _connect(path, user):
    comm = WebsocketCommunicator(application, path)
    comm.scope["user"] = user if user is not None else AnonymousUser()
    connected, code = await comm.connect()
    return comm, connected, code


async def _ws_case(station, user, consumer, action):
    """Run one WS matrix case; returns (connect_outputs, action_outputs or None)."""
    path = WS_PATHS[consumer](station.id)
    layer = get_channel_layer()
    agent = None
    spy = None
    if consumer == "control" and action != "connect":
        agent = _agent_comm(station.id)
        assert (await agent.connect())[0] is True
    if consumer == "terminal":
        # Stand-in for the agent's group so terminal_ensure has a receiver.
        spy = await layer.new_channel()
        await layer.group_add(f"terminal_{station.id}_agent", spy)

    comm, connected, code = await _connect(path, user)
    if not connected:
        return [{"type": "websocket.close", "code": code}], None
    connect_out = await _outputs(comm)
    if _closes(connect_out) or action == "connect":
        await comm.disconnect()
        if agent is not None:
            await agent.disconnect()
        return connect_out, None

    rid = "mx-1"
    if action == "lock_acquire":
        await comm.send_json_to({"type": "lock_acquire", "request_id": rid})
    elif action == "mic_open":
        # The audio protocol's mic_open carries no request_id (its frames neither).
        await comm.send_json_to({"type": "mic_open"})
    elif action.startswith("command:"):
        # The capability gate sits behind the lock: take it first (a no-op
        # forbidden for levels without control.use, which the command repeats).
        await comm.send_json_to({"type": "lock_acquire"})
        await _outputs(comm)
        await comm.send_json_to(
            {
                "type": "command",
                "request_id": rid,
                "slot": "slot0",
                "module": "fm0",
                "capability": action.split(":", 1)[1],
                "op": "set",
                "value": True,
            }
        )
    else:
        raise AssertionError(f"unknown WS action {action!r}")
    action_out = await _outputs(comm, timeout=0.6)
    if consumer == "control":
        await comm.send_json_to({"type": "lock_release"})
        await _outputs(comm)
    await comm.disconnect()
    if agent is not None:
        await agent.disconnect()
    return connect_out, action_out


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("level", LEVELS, ids=_level_id)
@pytest.mark.parametrize("entry", WS, ids=lambda e: f"{e[0]}-{e[3]}")
def test_ws_matrix(control_agent_auth, level, entry):
    consumer, area, verb, action = entry
    station = Station.objects.create(name=f"mx-{consumer}-{action}", status="online")
    user = _user(level)

    connect_out, action_out = asyncio.run(_ws_case(station, user, consumer, action))
    connect_frames = _frames(connect_out)

    if level is None:
        assert 4401 in _closes(connect_out), connect_out
        return

    if action == "connect":
        if _expected_allowed(level, area, verb):
            assert not _forbidden(connect_frames), connect_frames
            assert 4403 not in _closes(connect_out), connect_out
        else:
            assert _forbidden(connect_frames), connect_frames
            assert 4403 in _closes(connect_out), connect_out
        return

    # Action rows: the connect itself (control.view) must have succeeded.
    assert not _closes(connect_out), connect_out
    assert action_out is not None
    frames = _frames(action_out)
    rid = None if action == "mic_open" else "mx-1"
    if _expected_allowed(level, area, verb):
        assert not _forbidden(frames, rid), frames
        assert not _closes(action_out), action_out
    else:
        assert _forbidden(frames, rid), frames
