import pytest

from tests.test_api_read_fixtures import bearer, topology  # noqa: F401


def _seq():
    from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
    from apps.stations.models import StationTag

    seq, _ = RolloutSequence.objects.get_or_create(singleton_key="current")
    tag = StationTag.objects.create(name="early", slug="early")
    RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
    return seq


@pytest.mark.django_db
def test_rollout_sequence_list_member(topology):  # noqa: F811
    _seq()
    resp = bearer(topology["station_user"]).get("/api/v1/rollout-sequences/")
    assert resp.status_code == 200 and resp.data["count"] == 1
    assert resp.data["results"][0]["singleton_key"] == "current"


@pytest.mark.django_db
def test_rollout_sequence_entries_nested(topology):  # noqa: F811
    seq = _seq()
    resp = bearer(topology["station_user"]).get(f"/api/v1/rollout-sequences/{seq.pk}/entries/")
    assert resp.status_code == 200 and resp.data["count"] == 1


@pytest.mark.django_db
def test_rollout_applicant_403(topology):  # noqa: F811
    assert bearer(topology["applicant"]).get("/api/v1/rollout-sequences/").status_code == 403
