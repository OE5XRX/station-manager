"""manage.py enforce_retention: one count line per run, dry-run by default, compose service."""

import logging
from datetime import timedelta
from io import StringIO
from pathlib import Path

import pytest
import yaml
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.audit import retention
from apps.stations.models import StationAuditLog
from tests.test_retention import CATEGORIES

ROOT = Path(__file__).resolve().parent.parent
COMMAND = "python manage.py enforce_retention --loop --interval 86400"


def _old_row(station, user):
    row = StationAuditLog.objects.create(
        station=station,
        event_type=StationAuditLog.EventType.CONTROL_PTT,
        user=user,
        message="ptt",
    )
    StationAuditLog.objects.filter(pk=row.pk).update(
        created_at=timezone.now() - timedelta(days=400)
    )
    return row


def _run(*args):
    out, err = StringIO(), StringIO()
    call_command("enforce_retention", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


@pytest.mark.django_db
def test_command_dry_run_flag_changes_nothing(settings, station, member_user):
    settings.RETENTION_ENFORCE = True
    row = _old_row(station, member_user)
    out, _ = _run("--dry-run")
    assert out.startswith("enforce_retention[DRY-RUN]: ")
    assert "station_audit_deleted=1" in out
    assert "RETENTION_ENFORCE is false" not in out
    assert StationAuditLog.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_command_defaults_to_dry_run_when_not_enforced(settings, station, member_user):
    settings.RETENTION_ENFORCE = False
    row = _old_row(station, member_user)
    out, _ = _run()
    assert "enforce_retention[DRY-RUN]" in out
    assert "(RETENTION_ENFORCE is false)" in out
    assert StationAuditLog.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
def test_command_enforces_when_setting_true(settings, station, member_user, caplog):
    settings.RETENTION_ENFORCE = True
    row = _old_row(station, member_user)
    with caplog.at_level(logging.INFO, logger="apps.audit.retention"):
        out, err = _run()
    assert out.startswith("enforce_retention[ENFORCE]: ")
    assert "station_audit_deleted=1" in out
    assert out.strip().endswith("errors=none")
    assert err == ""
    assert not StationAuditLog.objects.filter(pk=row.pk).exists()
    assert out.strip() in [r.getMessage() for r in caplog.records]


@pytest.mark.django_db
def test_output_lists_every_category():
    out, _ = _run()
    lines = out.strip().splitlines()
    assert len(lines) == 1
    for key in CATEGORIES:
        assert f" {key}=" in lines[0]
    assert "station_audit_ip_scrubbed=" in lines[0]


@pytest.mark.django_db
def test_errors_go_to_stderr(monkeypatch):
    def boom(cfg, held):
        raise RuntimeError("boom")

    monkeypatch.setattr(retention, "_sso_audit_deleted", boom)
    out, err = _run()
    assert out == ""
    assert "errors=sso_audit_deleted" in err


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_interval_must_be_positive(value):
    with pytest.raises(CommandError, match="positive"):
        call_command("enforce_retention", "--interval", value, stdout=StringIO())


@pytest.mark.parametrize("path", ["docker-compose.yml", "deploy/docker-compose.prod.yml"])
def test_compose_has_retention_service(path):
    services = yaml.safe_load((ROOT / path).read_text())["services"]
    svc = services["retention"]
    assert svc["command"] == COMMAND
    # Same shape as the history maintainer; only the command differs.
    reference = dict(services["history-maintainer"])
    reference.pop("command")
    assert {k: v for k, v in svc.items() if k != "command"} == reference


def test_prod_compose_has_retention_service():
    svc = yaml.safe_load((ROOT / "deploy/docker-compose.prod.yml").read_text())["services"][
        "retention"
    ]
    assert "enforce_retention --loop --interval 86400" in svc["command"]
    assert "DJANGO_SETTINGS_MODULE=config.settings.prod" in svc["environment"]
