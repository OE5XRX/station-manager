"""Verify deployments 0007–0009: Deployment.image_release → result.image_release.

Old rows carry one ``image_release`` on the deployment. After the
migrations the deployment keeps the version (tag + channel) and every
result is pinned to the image the deployment used to carry.
"""

import pytest

BEFORE = [("deployments", "0006_deployment_target_group_name")]
AFTER = [("deployments", "0009_result_image_required")]


def _release(apps, tag, channel="release", machine="qemux86-64", **kw):
    return apps.get_model("images", "ImageRelease")._base_manager.create(
        tag=tag,
        machine=machine,
        channel=channel,
        s3_key=f"k/{tag}",
        sha256="a" * 64,
        size_bytes=1,
        **kw,
    )


@pytest.mark.django_db(transaction=True)
def test_backfill_moves_image_onto_results_and_version_onto_deployment(migrator):
    old = migrator.apply_initial_migration(BEFORE).apps
    Station = old.get_model("stations", "Station")
    Deployment = old.get_model("deployments", "Deployment")
    result_model = old.get_model("deployments", "DeploymentResult")

    dev = _release(old, "2026.10.01-01", channel="dev")
    rel = _release(old, "2026.09.01-01", machine="raspberrypi4-64")
    s1, s2, s3 = (Station.objects.create(name=n) for n in ("s1", "s2", "s3"))
    d_dev = Deployment.objects.create(image_release=dev, target_type="all")
    d_rel = Deployment.objects.create(image_release=rel, target_type="station", target_station=s3)
    Deployment.objects.create(image_release=rel, target_type="group")  # no results
    r1 = result_model.objects.create(deployment=d_dev, station=s1, status="success")
    r2 = result_model.objects.create(deployment=d_dev, station=s2, status="pending")
    r3 = result_model.objects.create(deployment=d_rel, station=s3, status="failed")

    new = migrator.apply_tested_migration(AFTER).apps
    new_deployment = new.get_model("deployments", "Deployment")
    new_result = new.get_model("deployments", "DeploymentResult")

    dep = new_deployment.objects.get(pk=d_dev.pk)
    assert (dep.image_tag, dep.image_channel, dep.skipped_stations) == (
        "2026.10.01-01",
        "dev",
        [],
    )
    dep = new_deployment.objects.get(pk=d_rel.pk)
    assert (dep.image_tag, dep.image_channel) == ("2026.09.01-01", "release")
    assert not new_deployment.objects.filter(image_tag="").exists()

    assert new_result.objects.get(pk=r1.pk).image_release_id == dev.pk
    assert new_result.objects.get(pk=r2.pk).image_release_id == dev.pk
    assert new_result.objects.get(pk=r3.pk).image_release_id == rel.pk
    field_names = {f.name for f in new_deployment._meta.get_fields()}
    assert "image_release" not in field_names
    assert new_result._meta.get_field("image_release").null is False


@pytest.mark.django_db(transaction=True)
def test_backfill_resolves_archived_release(migrator):
    """Archived releases stay resolvable — the backfill must not skip them."""
    from django.utils import timezone

    old = migrator.apply_initial_migration(BEFORE).apps
    Station = old.get_model("stations", "Station")
    Deployment = old.get_model("deployments", "Deployment")
    result_model = old.get_model("deployments", "DeploymentResult")
    archived = _release(old, "v0", archived_at=timezone.now())
    s = Station.objects.create(name="s")
    d = Deployment.objects.create(image_release=archived, target_type="all")
    r = result_model.objects.create(deployment=d, station=s)

    new = migrator.apply_tested_migration(AFTER).apps
    assert new.get_model("deployments", "Deployment").objects.get(pk=d.pk).image_tag == "v0"
    result = new.get_model("deployments", "DeploymentResult").objects.get(pk=r.pk)
    assert result.image_release_id == archived.pk


@pytest.mark.django_db(transaction=True)
def test_backfill_on_empty_tables_is_noop(migrator):
    migrator.apply_initial_migration(BEFORE)
    new = migrator.apply_tested_migration(AFTER).apps
    assert not new.get_model("deployments", "Deployment").objects.exists()


@pytest.mark.django_db(transaction=True)
def test_0009_is_explicitly_irreversible():
    """Migrating back to 0008 fails cleanly with IrreversibleError, schema untouched."""
    from django.db import connection
    from django.db.migrations.exceptions import IrreversibleError
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    try:
        with pytest.raises(IrreversibleError):
            executor.migrate([("deployments", "0008_backfill_version_fields")])
        # Nothing was reverted: 0009 still applied, result.image_release present.
        executor.loader.build_graph()
        assert ("deployments", "0009_result_image_required") in executor.loader.applied_migrations
    finally:
        # Leave the schema fully migrated for the rest of the session.
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
    columns = {
        c.name
        for c in connection.introspection.get_table_description(
            connection.cursor(), "deployments_deploymentresult"
        )
    }
    assert "image_release_id" in columns
