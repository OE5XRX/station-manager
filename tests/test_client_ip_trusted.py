"""Audit IPs come from the proxy-set X-Real-IP, never from client-controlled
X-Forwarded-For (re-review A1).

nginx (deploy/nginx.conf, / and /ws/) sets ``X-Real-IP $remote_addr`` and
*appends* to X-Forwarded-For, so the first XFF hop is whatever the client sent.
"""

from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from apps.accounts.client_ip import client_ip, client_ip_from_meta, ws_client_ip

SPOOF = "6.6.6.6"
REAL = "203.0.113.9"


def _req(**meta):
    return RequestFactory().get("/", **meta)


def test_x_real_ip_wins_and_xff_is_ignored():
    req = _req(
        REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP=REAL, HTTP_X_FORWARDED_FOR=f"{SPOOF}, {REAL}"
    )
    assert client_ip(req) == REAL


def test_xff_alone_is_ignored():
    assert client_ip(_req(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=SPOOF)) == "127.0.0.1"


def test_garbage_x_real_ip_falls_back_to_remote_addr():
    assert client_ip(_req(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="nope")) == "127.0.0.1"


def test_garbage_everything_is_none():
    assert client_ip(_req(REMOTE_ADDR="junk")) is None


def test_meta_mapping_variant():
    assert client_ip_from_meta({"HTTP_X_REAL_IP": REAL, "HTTP_X_FORWARDED_FOR": SPOOF}) == REAL
    assert client_ip_from_meta({"HTTP_X_FORWARDED_FOR": SPOOF, "REMOTE_ADDR": "10.0.0.1"}) == (
        "10.0.0.1"
    )


def test_ws_scope_variant():
    scope = {
        "headers": [(b"x-forwarded-for", SPOOF.encode()), (b"x-real-ip", REAL.encode())],
        "client": ("127.0.0.1", 1),
    }
    assert ws_client_ip(scope) == REAL
    assert (
        ws_client_ip(
            {"headers": [(b"x-forwarded-for", SPOOF.encode())], "client": ("127.0.0.1", 1)}
        )
        == "127.0.0.1"
    )
    assert (
        ws_client_ip({"headers": [(b"x-real-ip", b"bad")], "client": ("127.0.0.1", 1)})
        == "127.0.0.1"
    )
    assert ws_client_ip({}) is None


@pytest.mark.parametrize(
    "dotted",
    [
        "apps.api.audit._client_ip",
        "apps.stations.views._get_client_ip",
        "apps.deployments.views._get_client_ip",
        "apps.sso.views._client_ip",
        "apps.accounts.views_membership._get_client_ip",
        "apps.accounts.views._client_ip",
        "apps.accounts.permissions._client_ip",
    ],
)
def test_every_request_helper_ignores_spoofed_xff(dotted):
    import importlib

    module, name = dotted.rsplit(".", 1)
    helper = getattr(importlib.import_module(module), name)
    req = _req(REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP=REAL, HTTP_X_FORWARDED_FOR=SPOOF)
    assert helper(req) == REAL
    assert helper(_req(REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=SPOOF)) == "127.0.0.1"


def test_sso_validator_extract_ip_ignores_spoofed_xff():
    from apps.sso.permissions import SsoOAuth2Validator

    extract = SsoOAuth2Validator._extract_ip
    meta_style = SimpleNamespace(
        headers={"HTTP_X_FORWARDED_FOR": SPOOF, "HTTP_X_REAL_IP": REAL, "REMOTE_ADDR": "127.0.0.1"}
    )
    assert extract(meta_style) == REAL
    assert extract(SimpleNamespace(headers={"X-Forwarded-For": SPOOF, "X-Real-IP": REAL})) == REAL
    assert (
        extract(
            SimpleNamespace(headers={"HTTP_X_FORWARDED_FOR": SPOOF, "REMOTE_ADDR": "127.0.0.1"})
        )
        == "127.0.0.1"
    )
    assert extract(SimpleNamespace(headers={"HTTP_X_FORWARDED_FOR": SPOOF})) is None
