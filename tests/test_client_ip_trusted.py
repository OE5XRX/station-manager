"""Audit IPs come from the one trusted proxy header (``CLIENT_IP_HEADER``),
never from any other client-controllable header.

Prod is Cloudflare edge -> cloudflared -> web:8000 (no nginx): REMOTE_ADDR is
the cloudflared container, Cloudflare overwrites ``CF-Connecting-IP`` at the
edge, and cloudflared forwards every other client header verbatim — so
``X-Real-IP`` / ``X-Forwarded-For`` are attacker-chosen there.
"""

from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from apps.accounts.client_ip import client_ip, client_ip_from_meta, ws_client_ip

SPOOF = "6.6.6.6"
REAL = "203.0.113.9"
CLOUDFLARED = "172.18.0.4"


@pytest.fixture
def cf(settings):
    """Prod configuration: Cloudflare's header is the trusted one."""
    settings.CLIENT_IP_HEADER = "CF-Connecting-IP"


@pytest.fixture
def no_proxy(settings):
    """Dev configuration: no trusted header, only the socket peer."""
    settings.CLIENT_IP_HEADER = ""


def _req(**meta):
    return RequestFactory().get("/", **meta)


def test_prod_settings_default_to_cf_connecting_ip(tmp_path):
    """prod.py must trust CF-Connecting-IP without any servers-repo env var.

    Imported in a subprocess: prod.py mutates the OAUTH2_PROVIDER dict shared
    with base.py, which must not leak into this test run.
    """
    import os
    import subprocess
    import sys

    env = {k: v for k, v in os.environ.items() if k != "CLIENT_IP_HEADER"}
    env.update(DJANGO_SECRET_KEY="x", OIDC_RSA_KEY_PATH=str(tmp_path / "missing.pem"))
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import config.settings.prod as p;"
            "print(p.CLIENT_IP_HEADER);print(p.AXES_CLIENT_IP_CALLABLE)",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert out.stdout.split() == ["CF-Connecting-IP", "apps.accounts.client_ip.client_ip"]


def test_base_settings_default_to_no_trusted_header():
    from config.settings import base

    assert base.CLIENT_IP_HEADER == ""


def test_cf_header_honoured(cf):
    req = _req(REMOTE_ADDR=CLOUDFLARED, HTTP_CF_CONNECTING_IP=REAL)
    assert client_ip(req) == REAL


def test_spoofed_x_real_ip_and_xff_ignored_in_prod(cf):
    req = _req(
        REMOTE_ADDR=CLOUDFLARED,
        HTTP_CF_CONNECTING_IP=REAL,
        HTTP_X_REAL_IP=SPOOF,
        HTTP_X_FORWARDED_FOR=SPOOF,
    )
    assert client_ip(req) == REAL
    # Without the CF header a spoofed X-Real-IP still never wins.
    assert client_ip(_req(REMOTE_ADDR=CLOUDFLARED, HTTP_X_REAL_IP=SPOOF)) == CLOUDFLARED


def test_spoofed_cf_header_ignored_without_setting(no_proxy):
    req = _req(REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP=SPOOF, HTTP_X_REAL_IP=SPOOF)
    assert client_ip(req) == "127.0.0.1"


def test_garbage_cf_header_falls_back_to_remote_addr(cf):
    req = _req(REMOTE_ADDR=CLOUDFLARED, HTTP_CF_CONNECTING_IP="nope; DROP TABLE x--")
    assert client_ip(req) == CLOUDFLARED


def test_garbage_everything_is_none(cf):
    assert client_ip(_req(REMOTE_ADDR="junk", HTTP_CF_CONNECTING_IP="junk")) is None
    assert client_ip(None) is None


def test_meta_mapping_variant(cf):
    assert client_ip_from_meta({"HTTP_CF_CONNECTING_IP": REAL, "HTTP_X_REAL_IP": SPOOF}) == REAL
    assert client_ip_from_meta({"HTTP_X_REAL_IP": SPOOF, "REMOTE_ADDR": "10.0.0.1"}) == (
        "10.0.0.1"
    )


def test_ws_scope_honours_cf_header(cf):
    scope = {
        "headers": [
            (b"x-forwarded-for", SPOOF.encode()),
            (b"x-real-ip", SPOOF.encode()),
            (b"CF-Connecting-IP", REAL.encode()),
        ],
        "client": (CLOUDFLARED, 1),
    }
    assert ws_client_ip(scope) == REAL


def test_ws_scope_ignores_spoofed_headers(cf):
    scope = {"headers": [(b"x-real-ip", SPOOF.encode())], "client": (CLOUDFLARED, 1)}
    assert ws_client_ip(scope) == CLOUDFLARED
    garbage = {"headers": [(b"cf-connecting-ip", b"bad")], "client": (CLOUDFLARED, 1)}
    assert ws_client_ip(garbage) == CLOUDFLARED
    assert ws_client_ip({}) is None


def test_ws_scope_ignores_cf_header_without_setting(no_proxy):
    scope = {"headers": [(b"cf-connecting-ip", SPOOF.encode())], "client": ("127.0.0.1", 1)}
    assert ws_client_ip(scope) == "127.0.0.1"


REQUEST_HELPERS = [
    "apps.api.audit._client_ip",
    "apps.stations.views._get_client_ip",
    "apps.deployments.views._get_client_ip",
    "apps.sso.views._client_ip",
    "apps.accounts.views_membership._get_client_ip",
    "apps.accounts.views._client_ip",
    "apps.accounts.permissions._client_ip",
    "apps.accounts.client_ip.client_ip",
]


def _helper(dotted):
    import importlib

    module, name = dotted.rsplit(".", 1)
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize("dotted", REQUEST_HELPERS)
def test_every_request_helper_uses_trusted_header(cf, dotted):
    helper = _helper(dotted)
    req = _req(REMOTE_ADDR=CLOUDFLARED, HTTP_CF_CONNECTING_IP=REAL, HTTP_X_REAL_IP=SPOOF)
    assert helper(req) == REAL
    assert helper(_req(REMOTE_ADDR=CLOUDFLARED, HTTP_X_REAL_IP=SPOOF)) == CLOUDFLARED


@pytest.mark.parametrize("dotted", REQUEST_HELPERS)
def test_every_request_helper_ignores_cf_header_without_setting(no_proxy, dotted):
    req = _req(REMOTE_ADDR="127.0.0.1", HTTP_CF_CONNECTING_IP=SPOOF)
    assert _helper(dotted)(req) == "127.0.0.1"


def test_sso_validator_extract_ip_uses_trusted_header(cf):
    from apps.sso.permissions import SsoOAuth2Validator

    extract = SsoOAuth2Validator._extract_ip
    meta_style = SimpleNamespace(
        headers={
            "HTTP_X_REAL_IP": SPOOF,
            "HTTP_CF_CONNECTING_IP": REAL,
            "REMOTE_ADDR": CLOUDFLARED,
        }
    )
    assert extract(meta_style) == REAL
    assert extract(SimpleNamespace(headers={"X-Real-IP": SPOOF, "CF-Connecting-IP": REAL})) == REAL
    assert (
        extract(SimpleNamespace(headers={"HTTP_X_REAL_IP": SPOOF, "REMOTE_ADDR": CLOUDFLARED}))
        == CLOUDFLARED
    )
    assert extract(SimpleNamespace(headers={"X-Real-IP": SPOOF})) is None
    assert (
        extract(SimpleNamespace(headers={"HTTP_CF_CONNECTING_IP": "bad", "REMOTE_ADDR": "x"}))
        is None
    )


def test_sso_validator_extract_ip_ignores_cf_header_without_setting(no_proxy):
    from apps.sso.permissions import SsoOAuth2Validator

    extract = SsoOAuth2Validator._extract_ip
    assert (
        extract(
            SimpleNamespace(headers={"HTTP_CF_CONNECTING_IP": SPOOF, "REMOTE_ADDR": "127.0.0.1"})
        )
        == "127.0.0.1"
    )
    assert extract(SimpleNamespace(headers={"CF-Connecting-IP": SPOOF})) is None


@pytest.mark.django_db
def test_axes_records_trusted_client_ip(cf):
    """django-axes lockouts key on ip_address — must not be the shared cloudflared IP."""
    from axes.helpers import get_client_ip_address
    from django.test import override_settings

    with override_settings(AXES_CLIENT_IP_CALLABLE="apps.accounts.client_ip.client_ip"):
        req = _req(REMOTE_ADDR=CLOUDFLARED, HTTP_CF_CONNECTING_IP=REAL, HTTP_X_REAL_IP=SPOOF)
        assert get_client_ip_address(req) == REAL


def test_base_settings_route_axes_through_helper():
    from config.settings import base

    assert base.AXES_CLIENT_IP_CALLABLE == "apps.accounts.client_ip.client_ip"


@pytest.mark.django_db
def test_garbage_header_end_to_end_writes_fallback_row(cf):
    """A malformed trusted header must never cause a DB error on the audit row."""
    from django.contrib.auth import get_user_model

    from apps.accounts.models import AccountAuditLog
    from apps.accounts.permissions import AreaDenied, require

    user = get_user_model().objects.create_user(username="ipuser", password="x")
    req = _req(REMOTE_ADDR=CLOUDFLARED, HTTP_CF_CONNECTING_IP="999.1.1.1:abc")
    req.user = user
    with pytest.raises(AreaDenied):
        require(user, "audit", "view", request=req)
    assert AccountAuditLog.objects.get(event_type="permission_denied").ip_address == CLOUDFLARED
