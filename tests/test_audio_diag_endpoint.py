"""Tests for POST /api/v1/stations/{pk}/audio-diagnostics/ endpoint.

Task 9: orchestrator REST endpoint.
Orchestrator is monkeypatched — no real channel layer / agent needed.
Reuses topology + bearer fixtures from test_api_read_fixtures.
"""

import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_signal():
    return {"kind": "sine", "freq_hz": 1000, "level_dbfs": -20.0, "duration_ms": 500}


def _fake_report(anchor="U"):
    return {
        "anchor": anchor,
        "verdict": "ok",
        "taps": [],
        "stages": [],
        "static_gains": {},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_in_scope_user_gets_report(topology, monkeypatch):  # noqa: F811
    """station_user (in scope) → 200, report echoes anchor."""
    from apps.audio import orchestrator

    async def fake_run(station_id, anchor, signal, *, slot=0, timeout=15.0):
        return _fake_report(anchor)

    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = bearer(topology["station_user"])
    r = client.post(
        f"/api/v1/stations/{topology['station_in'].pk}/audio-diagnostics/",
        {"anchor": "U"},
        format="json",
    )
    assert r.status_code == 200
    assert r.json()["anchor"] == "U"


@pytest.mark.django_db
def test_out_of_scope_station_404(topology):  # noqa: F811
    """station_user posting to station_out (not assigned) → 404."""
    client = bearer(topology["station_user"])
    r = client.post(
        f"/api/v1/stations/{topology['station_out'].pk}/audio-diagnostics/",
        {"anchor": "U"},
        format="json",
    )
    assert r.status_code == 404


@pytest.mark.django_db
def test_applicant_forbidden(topology):  # noqa: F811
    """Applicant-level user → 403."""
    client = bearer(topology["applicant"])
    r = client.post(
        f"/api/v1/stations/{topology['station_in'].pk}/audio-diagnostics/",
        {"anchor": "U"},
        format="json",
    )
    assert r.status_code == 403


@pytest.mark.django_db
def test_agent_offline_returns_503(topology, monkeypatch):  # noqa: F811
    """AgentNotConnected → 503."""
    from apps.audio import orchestrator

    async def fake_run(station_id, anchor, signal, *, slot=0, timeout=15.0):
        raise orchestrator.AgentNotConnected()

    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = bearer(topology["station_user"])
    r = client.post(
        f"/api/v1/stations/{topology['station_in'].pk}/audio-diagnostics/",
        {"anchor": "U"},
        format="json",
    )
    assert r.status_code == 503
    assert "not connected" in r.json()["detail"]


@pytest.mark.django_db
def test_timeout_returns_504(topology, monkeypatch):  # noqa: F811
    """DiagnosticTimeout → 504."""
    from apps.audio import orchestrator

    async def fake_run(station_id, anchor, signal, *, slot=0, timeout=15.0):
        raise orchestrator.DiagnosticTimeout()

    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = bearer(topology["station_user"])
    r = client.post(
        f"/api/v1/stations/{topology['station_in'].pk}/audio-diagnostics/",
        {"anchor": "U"},
        format="json",
    )
    assert r.status_code == 504
    assert "timed out" in r.json()["detail"]


@pytest.mark.django_db
def test_bad_anchor_400(topology, monkeypatch):  # noqa: F811
    """anchor not in {U, C} → 400."""
    from apps.audio import orchestrator

    async def fake_run(station_id, anchor, signal, *, slot=0, timeout=15.0):
        return _fake_report(anchor)

    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = bearer(topology["station_user"])
    r = client.post(
        f"/api/v1/stations/{topology['station_in'].pk}/audio-diagnostics/",
        {"anchor": "Z"},
        format="json",
    )
    assert r.status_code == 400


@pytest.mark.django_db
def test_bad_slot_type_400(topology, monkeypatch):  # noqa: F811
    """slot must be int; string 'bad' → 400."""
    from apps.audio import orchestrator

    async def fake_run(station_id, anchor, signal, *, slot=0, timeout=15.0):
        return _fake_report(anchor)

    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = bearer(topology["station_user"])
    r = client.post(
        f"/api/v1/stations/{topology['station_in'].pk}/audio-diagnostics/",
        {"anchor": "C", "slot": "bad"},
        format="json",
    )
    assert r.status_code == 400


@pytest.mark.django_db
def test_admin_sees_any_station(topology, monkeypatch):  # noqa: F811
    """Admin can reach station_out as well."""
    from apps.audio import orchestrator

    async def fake_run(station_id, anchor, signal, *, slot=0, timeout=15.0):
        return _fake_report(anchor)

    monkeypatch.setattr(orchestrator, "run_headless_diagnostic", fake_run)
    client = bearer(topology["admin"])
    r = client.post(
        f"/api/v1/stations/{topology['station_out'].pk}/audio-diagnostics/",
        {"anchor": "C"},
        format="json",
    )
    assert r.status_code == 200
