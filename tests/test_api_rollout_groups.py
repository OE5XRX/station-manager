"""REST API for rollout groups (``/api/v1/rollout-groups/``) and the Station
``rollout_group`` field/filter.

Read needs ``stations.view`` (every member level incl. Applicant), writes need
``stations.manage`` (Staff/Admin) — same as StationTag. Every write audits
token origin. A group that is still referenced (station, sequence entry, MF
group/canary target) can't be deleted: 409, nothing changes.
"""

import pytest
from django.urls import reverse

from apps.accounts.models import AccountAuditLog
from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import RESERVED_TAG_SLUGS, RolloutGroup, Station, StationAuditLog
from tests.test_api_read_fixtures import session

pytestmark = pytest.mark.django_db

ROLES = ["admin", "staff", "region_mgr", "station_user", "applicant", "anon"]
READ_OK = {"admin", "staff", "region_mgr", "station_user", "applicant"}
WRITE_OK = {"admin", "staff"}


@pytest.fixture
def group():
    return RolloutGroup.objects.create(name="Canary", slug="canary")


def _client(api_topology, bearer, anon_client, role):
    if role == "anon":
        return anon_client
    if role == "applicant":
        # Applicants lack api_tokens.use; their PATs do not authenticate.
        return session(api_topology["applicant"])
    return bearer(api_topology[role])


def _assert_denied(resp, role):
    if role == "anon":
        assert resp.status_code in (401, 403)
    else:
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Permission matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
def test_list(api_topology, bearer, anon_client, group, role):
    resp = _client(api_topology, bearer, anon_client, role).get(reverse("api:rollout-group-list"))
    if role in READ_OK:
        assert resp.status_code == 200
        assert [g["slug"] for g in resp.data["results"]] == ["canary"]
    else:
        _assert_denied(resp, role)


@pytest.mark.parametrize("role", ROLES)
def test_retrieve(api_topology, bearer, anon_client, group, role):
    url = reverse("api:rollout-group-detail", args=[group.pk])
    resp = _client(api_topology, bearer, anon_client, role).get(url)
    if role in READ_OK:
        assert resp.status_code == 200
        assert set(resp.data) == {"id", "name", "slug", "channel", "description", "created_at"}
        assert resp.data["channel"] == "release"
    else:
        _assert_denied(resp, role)


@pytest.mark.parametrize("role", ROLES)
def test_create(api_topology, bearer, anon_client, role):
    resp = _client(api_topology, bearer, anon_client, role).post(
        reverse("api:rollout-group-list"),
        {"name": "Dev Boxes", "slug": "dev-boxes", "channel": "dev"},
        format="json",
    )
    if role in WRITE_OK:
        assert resp.status_code == 201, resp.data
        assert RolloutGroup.objects.get(slug="dev-boxes").channel == "dev"
        log = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED
        ).latest("created_at")
        assert log.message.startswith("RolloutGroup dev-boxes created via API token ")
    else:
        _assert_denied(resp, role)
        assert not RolloutGroup.objects.filter(slug="dev-boxes").exists()


@pytest.mark.parametrize("role", ROLES)
def test_update(api_topology, bearer, anon_client, group, role):
    url = reverse("api:rollout-group-detail", args=[group.pk])
    resp = _client(api_topology, bearer, anon_client, role).patch(
        url, {"description": "first wave"}, format="json"
    )
    group.refresh_from_db()
    if role in WRITE_OK:
        assert resp.status_code == 200, resp.data
        assert group.description == "first wave"
        log = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED
        ).latest("created_at")
        assert log.message.startswith("RolloutGroup canary updated via API token ")
    else:
        _assert_denied(resp, role)
        assert group.description == ""


@pytest.mark.parametrize("role", ROLES)
def test_delete(api_topology, bearer, anon_client, group, role):
    url = reverse("api:rollout-group-detail", args=[group.pk])
    resp = _client(api_topology, bearer, anon_client, role).delete(url)
    if role in WRITE_OK:
        assert resp.status_code == 204
        assert not RolloutGroup.objects.filter(pk=group.pk).exists()
        log = AccountAuditLog.objects.filter(
            event_type=AccountAuditLog.EventType.CONFIG_CHANGED
        ).latest("created_at")
        assert log.message.startswith("RolloutGroup canary deleted via API token ")
    else:
        _assert_denied(resp, role)
        assert RolloutGroup.objects.filter(pk=group.pk).exists()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(RESERVED_TAG_SLUGS))
def test_reserved_slug_400(api_topology, bearer, slug):
    resp = bearer(api_topology["staff"]).post(
        reverse("api:rollout-group-list"), {"name": "Sentinel", "slug": slug}, format="json"
    )
    assert resp.status_code == 400, resp.data
    assert "slug" in resp.data
    assert not RolloutGroup.objects.filter(slug=slug).exists()


def test_reserved_slug_on_update_400(api_topology, bearer, group):
    resp = bearer(api_topology["staff"]).patch(
        reverse("api:rollout-group-detail", args=[group.pk]),
        {"slug": "__unassigned__"},
        format="json",
    )
    assert resp.status_code == 400, resp.data
    assert "slug" in resp.data


def test_invalid_channel_400(api_topology, bearer):
    resp = bearer(api_topology["staff"]).post(
        reverse("api:rollout-group-list"),
        {"name": "Weird", "slug": "weird", "channel": "nightly"},
        format="json",
    )
    assert resp.status_code == 400, resp.data
    assert "channel" in resp.data
    assert not RolloutGroup.objects.filter(slug="weird").exists()


def test_api_audit_rows_carry_details(api_topology, bearer):
    client = bearer(api_topology["staff"])
    resp = client.post(
        reverse("api:rollout-group-list"),
        {"name": "Dev", "slug": "dev", "channel": "dev"},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    rows = AccountAuditLog.objects.filter(event_type=AccountAuditLog.EventType.CONFIG_CHANGED)
    assert rows.get().details["changes"] == {"channel": "dev"}

    client.patch(
        reverse("api:rollout-group-detail", args=[resp.data["id"]]),
        {"slug": "dev-boxes"},
        format="json",
    )
    log = rows.latest("pk")
    assert log.message.startswith("RolloutGroup dev-boxes updated via API token ")
    assert log.details["changes"] == {"slug": {"old": "dev", "new": "dev-boxes"}}


def test_delete_race_integrity_error_is_409(api_topology, bearer, group, monkeypatch):
    from django.db import IntegrityError

    def racing_delete(self, *a, **kw):
        self.pk = None  # Collector.delete() nulls the pk before the deferred FK fires
        raise IntegrityError("deferred FK violation")

    monkeypatch.setattr(RolloutGroup, "delete", racing_delete)
    before = AccountAuditLog.objects.count()
    resp = bearer(api_topology["staff"]).delete(
        reverse("api:rollout-group-detail", args=[group.pk])
    )
    assert resp.status_code == 409, resp.data
    assert AccountAuditLog.objects.count() == before


def test_channel_is_fixed_after_create(api_topology, bearer, group):
    url = reverse("api:rollout-group-detail", args=[group.pk])
    client = bearer(api_topology["staff"])

    resp = client.patch(url, {"channel": "dev"}, format="json")
    assert resp.status_code == 400, resp.data
    assert "channel" in resp.data
    group.refresh_from_db()
    assert group.channel == RolloutGroup.Channel.RELEASE

    # Echoing the unchanged channel (full PUT) is fine.
    resp = client.put(
        url, {"name": "Canary", "slug": "canary", "channel": "release"}, format="json"
    )
    assert resp.status_code == 200, resp.data


# ---------------------------------------------------------------------------
# Delete is blocked while the group is referenced (all FKs PROTECT)
# ---------------------------------------------------------------------------


def _ref_station(api_topology, g):
    station = api_topology["station_in"]
    station.rollout_group = g
    station.save(update_fields=["rollout_group"])


def _ref_sequence(api_topology, g):
    seq, _ = RolloutSequence.objects.get_or_create(singleton_key="current")
    RolloutSequenceEntry.objects.create(sequence=seq, group=g, position=0)


def _ref_mf(api_topology, g, **kw):
    from apps.module_firmware.models import ModuleFirmwareTarget, ModuleType

    fm = ModuleType.objects.create(key="fm", display_name="FM")
    ModuleFirmwareTarget.objects.create(module_type=fm, version="1.0", **kw)


@pytest.mark.parametrize(
    ("make_ref", "key"),
    [
        (_ref_station, "stations=1"),
        (_ref_sequence, "sequence_entries=1"),
        (lambda t, g: _ref_mf(t, g, scope="group", group=g), "firmware_targets=1"),
        (lambda t, g: _ref_mf(t, g, scope="fleet", canary_group=g), "canary_targets=1"),
    ],
)
def test_delete_blocked_while_referenced(api_topology, bearer, group, make_ref, key):
    make_ref(api_topology, group)
    before = AccountAuditLog.objects.count()

    resp = bearer(api_topology["staff"]).delete(
        reverse("api:rollout-group-detail", args=[group.pk])
    )

    assert resp.status_code == 409, resp.data
    assert key in resp.data["detail"]
    assert RolloutGroup.objects.filter(pk=group.pk).exists()
    assert AccountAuditLog.objects.count() == before  # "deleted" row rolled back
    assert not StationAuditLog.objects.filter(
        event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    ).exists()


# ---------------------------------------------------------------------------
# Station.rollout_group field + filter
# ---------------------------------------------------------------------------


def test_station_read_exposes_rollout_group(api_topology, bearer, group):
    station = api_topology["station_in"]
    station.rollout_group = group
    station.save(update_fields=["rollout_group"])
    resp = bearer(api_topology["station_user"]).get(
        reverse("api:station-detail", args=[station.pk])
    )
    assert resp.status_code == 200
    assert resp.data["rollout_group"] == group.pk


def test_station_patch_rollout_group_audits(api_topology, bearer, group):
    staff = api_topology["staff"]
    station = api_topology["station_in"]
    url = reverse("api:station-detail", args=[station.pk])

    resp = bearer(staff).patch(url, {"rollout_group": group.pk}, format="json")
    assert resp.status_code == 200, resp.data
    station.refresh_from_db()
    assert station.rollout_group == group
    logs = StationAuditLog.objects.filter(
        station=station, event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    )
    assert logs.count() == 1
    log = logs.get()
    assert log.message.startswith(f"Rollout group: none → Canary by {staff} via API token ")
    assert log.changes == {"rollout_group": {"old": "none", "new": "Canary"}}
    assert log.user == staff

    # Unchanged group (and unrelated field edits) → no further group audit row.
    resp = bearer(staff).patch(url, {"rollout_group": group.pk, "notes": "hi"}, format="json")
    assert resp.status_code == 200
    assert logs.count() == 1

    # Clearing the group is audited too.
    resp = bearer(staff).patch(url, {"rollout_group": None}, format="json")
    assert resp.status_code == 200
    assert logs.count() == 2
    assert logs.latest("created_at").message.startswith(
        f"Rollout group: Canary → none by {staff} via API token "
    )


def test_station_create_with_rollout_group_audits(api_topology, bearer, group):
    staff = api_topology["staff"]
    resp = bearer(staff).post(
        reverse("api:station-list"),
        {"name": "S-new", "callsign": "OE1NEW", "rollout_group": group.pk},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    station = Station.objects.get(pk=resp.data["id"])
    assert station.rollout_group == group
    log = StationAuditLog.objects.get(
        station=station, event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    )
    assert log.message.startswith(f"Rollout group: none → Canary by {staff} via API token ")


def test_station_create_without_group_no_group_audit(api_topology, bearer):
    resp = bearer(api_topology["staff"]).post(
        reverse("api:station-list"), {"name": "S-plain", "callsign": "OE1PLN"}, format="json"
    )
    assert resp.status_code == 201, resp.data
    assert not StationAuditLog.objects.filter(
        station_id=resp.data["id"], event_type=StationAuditLog.EventType.ROLLOUT_GROUP_CHANGED
    ).exists()


def test_station_patch_rollout_group_denied_for_member(api_topology, bearer, group):
    station = api_topology["station_in"]
    resp = bearer(api_topology["station_user"]).patch(
        reverse("api:station-detail", args=[station.pk]),
        {"rollout_group": group.pk},
        format="json",
    )
    assert resp.status_code == 403
    station.refresh_from_db()
    assert station.rollout_group is None


def test_station_patch_rollout_group_denied_for_region_manager(api_topology, bearer, group):
    # Station→group assignment is staff/admin only, even in the manager's own region.
    station = api_topology["station_in"]
    resp = bearer(api_topology["region_mgr"]).patch(
        reverse("api:station-detail", args=[station.pk]),
        {"rollout_group": group.pk},
        format="json",
    )
    assert resp.status_code == 403
    station.refresh_from_db()
    assert station.rollout_group is None


def test_station_filter_by_rollout_group(api_topology, bearer, group):
    other = RolloutGroup.objects.create(name="Other", slug="other")
    api_topology["station_in"].rollout_group = group
    api_topology["station_in"].save(update_fields=["rollout_group"])
    api_topology["station_out"].rollout_group = other
    api_topology["station_out"].save(update_fields=["rollout_group"])

    client = bearer(api_topology["station_user"])
    url = reverse("api:station-list")
    ids = {s["id"] for s in client.get(url, {"rollout_group": group.pk}).data["results"]}
    assert ids == {api_topology["station_in"].pk}
    ids = {s["id"] for s in client.get(url, {"rollout_group": other.pk}).data["results"]}
    assert ids == {api_topology["station_out"].pk}


# ---------------------------------------------------------------------------
# OpenAPI
# ---------------------------------------------------------------------------


def test_schema_lists_rollout_groups_paths(api_topology, bearer):
    resp = bearer(api_topology["admin"]).get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    paths = resp.data["paths"]
    assert {"get", "post"} <= set(paths["/api/v1/rollout-groups/"])
    assert {"get", "put", "patch", "delete"} <= set(paths["/api/v1/rollout-groups/{id}/"])
    schemas = resp.data["components"]["schemas"]
    assert set(schemas["RolloutGroup"]["properties"]) == {
        "id",
        "name",
        "slug",
        "channel",
        "description",
        "created_at",
    }
    assert "rollout_group" in schemas["Station"]["properties"]
    station_params = {p["name"] for p in paths["/api/v1/stations/"]["get"]["parameters"]}
    assert "rollout_group" in station_params


def test_api_noop_update_writes_no_audit_row(api_topology, bearer, group):
    before = AccountAuditLog.objects.count()
    resp = bearer(api_topology["staff"]).patch(
        reverse("api:rollout-group-detail", args=[group.pk]), {"name": "Canary"}, format="json"
    )
    assert resp.status_code == 200, resp.data
    assert AccountAuditLog.objects.count() == before
