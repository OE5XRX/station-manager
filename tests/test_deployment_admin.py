import pytest
from django.urls import reverse

from apps.deployments.models import DeploymentResult


@pytest.mark.django_db
def test_result_admin_saves_when_release_archived(
    client, admin_user, deployment_result, image_release
):
    image_release.archive()
    client.force_login(admin_user)
    url = reverse("admin:deployments_deploymentresult_change", args=[deployment_result.pk])

    page = client.get(url)
    assert page.status_code == 200
    form = page.context["adminform"].form
    assert "image_release" not in form.fields

    data = {
        "deployment": deployment_result.deployment_id,
        "station": deployment_result.station_id,
        "status": DeploymentResult.Status.FAILED,
        "error_message": "fixed by hand",
        "previous_version": "",
        "new_version": "",
    }
    response = client.post(url, data)

    assert response.status_code == 302, response.context["adminform"].form.errors
    deployment_result.refresh_from_db()
    assert deployment_result.status == DeploymentResult.Status.FAILED
    assert deployment_result.image_release_id == image_release.pk
