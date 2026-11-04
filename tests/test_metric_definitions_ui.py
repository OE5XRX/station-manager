"""UI: metric management page (Task 6)."""

import re

import pytest
from django.urls import reverse
from django.utils import timezone, translation

from apps.accounts.models import AccountAuditLog
from apps.monitoring.models import MetricDefinition


@pytest.fixture
def mdef(db):
    obj, _ = MetricDefinition.objects.update_or_create(
        key="pwr.12v.voltage_v",
        defaults={
            "label": "12 V rail voltage",
            "unit": "V",
            "group": "power",
            "precision": 2,
            "min_value": 0.0,
            "max_value": 30.0,
            "last_rejected_at": timezone.now(),
            "last_rejected_value": {"min": 31.5, "max": 31.5, "avg": 31.5},
        },
    )
    return obj


def _post(**overrides):
    data = {
        "label_override": "",
        "min_override": "",
        "max_override": "",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
class TestMetricDefinitionsPage:
    def test_lists_definitions_with_rejected_badge(self, client, admin_user, mdef):
        client.force_login(admin_user)
        with translation.override("de"):
            resp = client.get(reverse("monitoring:metric_definitions"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "pwr.12v.voltage_v" in content
        assert "12 V rail voltage" in content
        assert "data-rejected" in content
        assert "31.5" in content
        assert "{#" not in content and "#}" not in content
        assert 'value="None"' not in content
        for tag in re.findall(r"<input[^>]*type=\"number\"[^>]*>", content):
            assert 'lang="en"' in tag and 'step="any"' in tag, tag

    def test_group_filter(self, client, admin_user, mdef):
        client.force_login(admin_user)
        resp = client.get(reverse("monitoring:metric_definitions"), {"group": "disk"})
        assert resp.status_code == 200
        keys = [d.key for d in resp.context["definitions"]]
        assert "pwr.12v.voltage_v" not in keys
        assert all(k.startswith("disk.") for k in keys)

    def test_viewer_can_view_readonly(self, client, staff_user, mdef):
        client.force_login(staff_user)
        resp = client.get(reverse("monitoring:metric_definitions"))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "pwr.12v.voltage_v" in content
        assert reverse("monitoring:metric_definition_update", args=[mdef.pk]) not in content

    def test_applicant_can_view(self, client, applicant_user, mdef):
        # alert_rules.view = applicant (permissions.AREAS)
        client.force_login(applicant_user)
        assert client.get(reverse("monitoring:metric_definitions")).status_code == 200


@pytest.mark.django_db
class TestMetricDefinitionUpdate:
    def test_manager_sets_overrides(self, client, admin_user, mdef):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(
                label_override="12-V-Schiene",
                min_override="9.5",
                max_override="32",
                is_hidden="on",
            ),
        )
        assert resp.status_code == 302
        mdef.refresh_from_db()
        assert mdef.label_override == "12-V-Schiene"
        assert mdef.min_override == 9.5
        assert mdef.max_override == 32.0
        assert mdef.is_hidden is True
        assert mdef.is_blocked is False
        assert mdef.display_label == "12-V-Schiene"
        assert AccountAuditLog.objects.filter(details__object="metric_definition").exists()

    def test_min_greater_than_max_rejected(self, client, admin_user, mdef):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(min_override="20", max_override="10"),
        )
        assert resp.status_code == 302
        mdef.refresh_from_db()
        assert mdef.min_override is None
        assert mdef.max_override is None

    def test_agent_fields_not_writable(self, client, admin_user, mdef):
        client.force_login(admin_user)
        client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(label="hacked", unit="kV", key="x.y"),
        )
        mdef.refresh_from_db()
        assert mdef.label == "12 V rail voltage"
        assert mdef.unit == "V"
        assert mdef.key == "pwr.12v.voltage_v"

    def test_viewer_cannot_post(self, client, staff_user, mdef):
        client.force_login(staff_user)
        resp = client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(is_blocked="on"),
        )
        assert resp.status_code == 403
        mdef.refresh_from_db()
        assert mdef.is_blocked is False

    def test_next_redirect_is_safe(self, client, admin_user, mdef):
        client.force_login(admin_user)
        resp = client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(next="https://evil.example/"),
        )
        assert resp.status_code == 302
        assert "evil.example" not in resp["Location"]
        resp = client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(next=reverse("monitoring:metric_definitions") + "?group=power"),
        )
        assert "?group=power" in resp["Location"]


@pytest.mark.django_db
class TestMetricDefinitionReviewRound2:
    def test_min_override_alone_above_agent_max_rejected(self, client, admin_user, mdef):
        client.force_login(admin_user)
        client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(min_override="40"),
        )
        mdef.refresh_from_db()
        assert mdef.min_override is None

    def test_form_error_on_set_override_field(self, mdef):
        from apps.monitoring.forms import MetricDefinitionForm

        form = MetricDefinitionForm(_post(max_override="-1"), instance=mdef)
        assert not form.is_valid()
        assert "max_override" in form.errors
        form = MetricDefinitionForm(_post(min_override="40"), instance=mdef)
        assert not form.is_valid()
        assert "min_override" in form.errors

    def test_update_writes_only_operator_fields(self, client, admin_user, mdef, monkeypatch):
        from apps.monitoring import views

        stale = MetricDefinition.objects.get(pk=mdef.pk)
        # A heartbeat updated the agent fields after the view fetched the row.
        MetricDefinition.objects.filter(pk=mdef.pk).update(label="new agent label", unit="mV")
        monkeypatch.setattr(views, "get_object_or_404", lambda *a, **kw: stale)
        client.force_login(admin_user)
        client.post(
            reverse("monitoring:metric_definition_update", args=[mdef.pk]),
            _post(label_override="Rail"),
        )
        mdef.refresh_from_db()
        assert mdef.label_override == "Rail"
        assert mdef.label == "new agent label"
        assert mdef.unit == "mV"
