import pytest
from django.urls import reverse

from apps.deployments.models import Deployment
from tests.test_api_read_fixtures import client_for, topology  # noqa: F401


def _post(client, image_release, **extra):
    data = {
        "version": f"{image_release.channel}|{image_release.tag}",
        "target_type": Deployment.TargetType.GROUP,
        "strategy": Deployment.Strategy.IMMEDIATE,
        "phase_config": "{}",
    }
    data.update(extra)
    return client.post(reverse("deployments:deployment_create"), data=data)


@pytest.mark.django_db
def test_create_by_group_targets_group_stations(
    client, staff_user, image_release, station_factory, make_rollout_group
):
    group = make_rollout_group("canary")
    in_group = station_factory(rollout_group=group, current_image_release=image_release)
    station_factory(current_image_release=image_release)  # ungrouped, must not be targeted
    client.force_login(staff_user)

    response = _post(client, image_release, target_group=group.pk)

    assert response.status_code == 302
    dep = Deployment.objects.latest("created_at")
    assert dep.target_group == group
    assert [r.station for r in dep.results.all()] == [in_group]


@pytest.mark.django_db
def test_by_group_requires_group(client, staff_user, image_release):
    client.force_login(staff_user)
    response = _post(client, image_release)
    assert response.status_code == 200
    assert "A target group is required when target type is &#x27;By Group&#x27;." in (
        response.content.decode()
    )
    assert not Deployment.objects.exists()


@pytest.mark.django_db
def test_detail_shows_group_and_deleted_group_label(
    client, staff_user, image_release, make_rollout_group
):
    group = make_rollout_group("canary")
    dep = Deployment.objects.create(
        image_tag=image_release.tag,
        image_channel=image_release.channel,
        target_type=Deployment.TargetType.GROUP,
        target_group=group,
    )
    client.force_login(staff_user)
    url = reverse("deployments:deployment_detail", kwargs={"pk": dep.pk})
    assert "By Group: canary" in client.get(url).content.decode()

    assert dep.target_group_name == "canary"

    # Rename: history keeps the deploy-time name.
    group.name = "renamed"
    group.save()
    assert "By Group: canary" in client.get(url).content.decode()

    group.delete()
    dep.refresh_from_db()
    assert dep.target_group is None
    assert "By Group: canary (deleted)" in client.get(url).content.decode()
    assert (
        "By Group: canary (deleted)"
        in client.get(reverse("deployments:deployment_list")).content.decode()
    )


@pytest.mark.django_db
def test_group_less_legacy_deployment_shows_deleted_label(client, staff_user, image_release):
    # A tag deployment whose tag was already gone before the migration.
    dep = Deployment.objects.create(
        image_tag=image_release.tag,
        image_channel=image_release.channel,
        target_type=Deployment.TargetType.GROUP,
    )
    client.force_login(staff_user)
    url = reverse("deployments:deployment_detail", kwargs={"pk": dep.pk})
    assert "By Group (deleted)" in client.get(url).content.decode()


@pytest.mark.django_db
def test_api_deployment_exposes_target_group(topology, image_release, make_rollout_group):  # noqa: F811
    group = make_rollout_group("canary")
    dep = Deployment.objects.create(
        image_tag=image_release.tag,
        image_channel=image_release.channel,
        target_type=Deployment.TargetType.GROUP,
        target_group=group,
    )
    client = client_for(topology["staff"])
    data = client.get(f"/api/v1/deployments/{dep.pk}/").data
    assert data["target_group"] == group.pk
    assert "target_tag" not in data
