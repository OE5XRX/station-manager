"""Verify rollouts.0005_tags_to_groups: functional StationTags become RolloutGroups.

django_test_migrations runs the data migration against the historical
schema. Most tests start at the leaf migrations *before* the additive
group schema; the collision test starts right before 0005 so it can
seed a pre-existing RolloutGroup.
"""

import pytest

PRE_SCHEMA = [
    ("stations", "0027_station_agent_capabilities"),
    ("rollouts", "0003_rolloutsequence_singleton_key"),
    ("deployments", "0003_reconcile_orphaned_results"),
    ("module_firmware", "0013_convergence_transient_failures"),
]
PRE_DATA = [
    ("stations", "0028_rolloutgroup"),
    ("rollouts", "0004_entry_group"),
    ("deployments", "0004_target_group"),
    ("module_firmware", "0014_target_group"),
]
TESTED = [("rollouts", "0005_tags_to_groups")]


class _Seed:
    """Helpers that create rows through a historical app registry."""

    def __init__(self, apps):
        self.apps = apps
        self.Tag = apps.get_model("stations", "StationTag")
        self.Station = apps.get_model("stations", "Station")
        self.Seq = apps.get_model("rollouts", "RolloutSequence")
        self.Entry = apps.get_model("rollouts", "RolloutSequenceEntry")
        self.Release = apps.get_model("images", "ImageRelease")
        self.Deployment = apps.get_model("deployments", "Deployment")
        self.ModuleType = apps.get_model("module_firmware", "ModuleType")
        self.Target = apps.get_model("module_firmware", "ModuleFirmwareTarget")
        self._seq = None
        self._release = None

    def tag(self, name, **kw):
        return self.Tag.objects.create(name=name, slug=kw.pop("slug", name), **kw)

    def station(self, name, *tags):
        s = self.Station.objects.create(name=name)
        s.tags.set(tags)
        return s

    def entry(self, tag, position):
        if self._seq is None:
            self._seq, _ = self.Seq.objects.get_or_create(singleton_key="current")
        return self.Entry.objects.create(sequence=self._seq, tag=tag, position=position)

    def deployment(self, tag, target_type="tag"):
        if self._release is None:
            self._release = self.Release.objects.create(
                tag="v1", machine="qemux86-64", s3_key="k", sha256="a" * 64, size_bytes=1
            )
        return self.Deployment.objects.create(
            image_release=self._release, target_type=target_type, target_tag=tag
        )

    def module_type(self, key="fm"):
        return self.ModuleType.objects.create(key=key, display_name=key)


def _groups(apps):
    return apps.get_model("stations", "RolloutGroup").objects


def _audits(apps):
    return apps.get_model("stations", "StationAuditLog").objects.filter(
        event_type="rollout_group_changed"
    )


@pytest.mark.django_db(transaction=True)
def test_station_with_zero_tags_gets_no_group(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    seed.entry(seed.tag("beta"), 0)
    seed.station("bare")

    new = migrator.apply_tested_migration(TESTED).apps
    station = new.get_model("stations", "Station").objects.get(name="bare")
    assert station.rollout_group_id is None
    assert not _audits(new).exists()


@pytest.mark.django_db(transaction=True)
def test_station_with_one_functional_tag_gets_that_group(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    beta = seed.tag("Beta", slug="beta", description="beta testers")
    seed.entry(beta, 0)
    seed.station("s1", beta)

    new = migrator.apply_tested_migration(TESTED).apps
    group = _groups(new).get()
    assert (group.name, group.slug, group.channel, group.description) == (
        "Beta",
        "beta",
        "release",
        "beta testers",
    )
    entry = new.get_model("rollouts", "RolloutSequenceEntry").objects.get()
    assert entry.group_id == group.pk
    assert entry.position == 0
    station = new.get_model("stations", "Station").objects.get(name="s1")
    assert station.rollout_group_id == group.pk
    assert not _audits(new).exists()


@pytest.mark.django_db(transaction=True)
def test_station_with_n_tags_gets_frontmost_and_audit(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    a, b, c = seed.tag("a"), seed.tag("b"), seed.tag("c")
    seed.entry(b, 0)
    seed.entry(a, 1)
    seed.deployment(c)
    seed.station("multi", a, b, c)

    new = migrator.apply_tested_migration(TESTED).apps
    station = new.get_model("stations", "Station").objects.get(name="multi")
    assert station.rollout_group.slug == "b"
    audits = list(_audits(new))
    assert len(audits) == 1
    audit = audits[0]
    assert audit.station_id == station.pk
    assert audit.user_id is None
    assert audit.message == (
        "Migration: functional tags ['a', 'b', 'c'] → rollout group 'b' (front-most in sequence)"
    )


@pytest.mark.django_db(transaction=True)
def test_tie_break_by_name_for_tags_outside_sequence(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    zeta, alpha = seed.tag("zeta"), seed.tag("alpha")
    seed.deployment(zeta)
    seed.deployment(alpha)
    seed.station("s", zeta, alpha)

    new = migrator.apply_tested_migration(TESTED).apps
    station = new.get_model("stations", "Station").objects.get(name="s")
    assert station.rollout_group.slug == "alpha"
    assert _audits(new).count() == 1


@pytest.mark.django_db(transaction=True)
def test_label_only_tags_create_no_group(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    label = seed.tag("hamnet")
    seed.station("s", label)

    new = migrator.apply_tested_migration(TESTED).apps
    assert not _groups(new).exists()
    station = new.get_model("stations", "Station").objects.get(name="s")
    assert station.rollout_group_id is None
    assert list(station.tags.values_list("slug", flat=True)) == ["hamnet"]


@pytest.mark.django_db(transaction=True)
def test_deployment_and_mf_targets_remapped(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    dep_tag, mf_tag, canary = seed.tag("dep"), seed.tag("mf"), seed.tag("canary")
    dep = seed.deployment(dep_tag)
    other = seed.deployment(None, target_type="all")
    mt = seed.module_type()
    tag_target = seed.Target.objects.create(module_type=mt, version="1.0", scope="tag", tag=mf_tag)
    fleet_target = seed.Target.objects.create(
        module_type=mt, version="1.1", scope="fleet", canary_tag=canary
    )

    new = migrator.apply_tested_migration(TESTED).apps
    groups = {g.slug: g.pk for g in _groups(new).all()}
    assert set(groups) == {"dep", "mf", "canary"}

    Deployment = new.get_model("deployments", "Deployment")  # noqa: N806
    dep = Deployment.objects.get(pk=dep.pk)
    assert dep.target_type == "group"
    assert dep.target_group_id == groups["dep"]
    assert dep.target_tag_id is not None  # old field untouched in this step
    other = Deployment.objects.get(pk=other.pk)
    assert other.target_type == "all"
    assert other.target_group_id is None

    Target = new.get_model("module_firmware", "ModuleFirmwareTarget")  # noqa: N806
    tag_target = Target.objects.get(pk=tag_target.pk)
    assert tag_target.scope == "group"
    assert tag_target.group_id == groups["mf"]
    fleet_target = Target.objects.get(pk=fleet_target.pk)
    assert fleet_target.scope == "fleet"
    assert fleet_target.group_id is None
    assert fleet_target.canary_group_id == groups["canary"]


@pytest.mark.django_db(transaction=True)
def test_migration_survives_orphans_and_collisions(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_DATA).apps)
    Group = seed.apps.get_model("stations", "RolloutGroup")  # noqa: N806
    # Pre-existing groups: one with the same slug (re-run → reuse), one
    # stealing the name of another tag (→ suffixed name).
    reused = Group.objects.create(name="Reused", slug="reuse", channel="dev")
    Group.objects.create(name="Clash", slug="something-else")
    Group.objects.create(name="Clash (clash)", slug="something-else-2")

    reuse_tag = seed.tag("reuse")
    clash_tag = seed.tag("Clash", slug="clash")
    seed.entry(reuse_tag, 0)
    seed.entry(clash_tag, 1)
    # Dirty data: tag deployment with no tag; fleet target whose canary
    # tag was deleted (SET_NULL → NULL).
    orphan_dep = seed.deployment(None, target_type="tag")
    mt = seed.module_type()
    doomed = seed.tag("doomed")
    target = seed.Target.objects.create(
        module_type=mt, version="1.0", scope="fleet", canary_tag=doomed
    )
    doomed.delete()
    # Station already in a group must not be overwritten.
    pinned = seed.station("pinned", clash_tag)
    pinned.rollout_group = reused
    pinned.save()

    new = migrator.apply_tested_migration(TESTED).apps
    groups = _groups(new)
    assert groups.get(slug="reuse").pk == reused.pk
    assert groups.get(slug="reuse").channel == "dev"
    assert groups.get(slug="clash").name == "Clash (clash) 2"
    orphan_dep = new.get_model("deployments", "Deployment").objects.get(pk=orphan_dep.pk)
    assert orphan_dep.target_type == "group"
    assert orphan_dep.target_group_id is None
    target = new.get_model("module_firmware", "ModuleFirmwareTarget").objects.get(pk=target.pk)
    assert target.canary_group_id is None
    pinned = new.get_model("stations", "Station").objects.get(name="pinned")
    assert pinned.rollout_group_id == reused.pk
    assert not _audits(new).exists()


@pytest.mark.django_db(transaction=True)
def test_tags_unchanged(migrator):
    seed = _Seed(migrator.apply_initial_migration(PRE_SCHEMA).apps)
    a, b, label = seed.tag("a"), seed.tag("b"), seed.tag("label")
    seed.entry(a, 0)
    seed.deployment(b)
    s1 = seed.station("s1", a, b, label)
    s2 = seed.station("s2", label)
    before = {
        s.pk: sorted(s.tags.values_list("slug", flat=True)) for s in seed.Station.objects.all()
    }

    new = migrator.apply_tested_migration(TESTED).apps
    Station = new.get_model("stations", "Station")  # noqa: N806
    assert new.get_model("stations", "StationTag").objects.count() == 3
    after = {s.pk: sorted(s.tags.values_list("slug", flat=True)) for s in Station.objects.all()}
    assert after == before
    assert after[s1.pk] == ["a", "b", "label"]
    assert after[s2.pk] == ["label"]


@pytest.mark.django_db
def test_drop_entry_tag_deletes_groupless_entries_and_renumbers(migrator):
    """0006 must never abort on an entry 0005 left without a group: it drops
    it, closes the position gap, and the column becomes NOT NULL."""
    old = migrator.apply_initial_migration([("rollouts", "0005_tags_to_groups")]).apps
    seed = _Seed(old)
    group_model = old.get_model("stations", "RolloutGroup")
    g0 = group_model.objects.create(name="g0", slug="g0")
    g2 = group_model.objects.create(name="g2", slug="g2")
    e0 = seed.entry(seed.tag("t0"), 0)
    seed.entry(seed.tag("t1"), 1)  # never mapped -> group stays NULL
    e2 = seed.entry(seed.tag("t2"), 2)
    seed.Entry.objects.filter(pk=e0.pk).update(group=g0)
    seed.Entry.objects.filter(pk=e2.pk).update(group=g2)

    new = migrator.apply_tested_migration([("rollouts", "0006_drop_entry_tag")]).apps

    entry_model = new.get_model("rollouts", "RolloutSequenceEntry")
    assert list(entry_model.objects.order_by("position").values_list("group_id", "position")) == [
        (g0.pk, 0),
        (g2.pk, 1),
    ]
    assert not entry_model._meta.get_field("group").null


@pytest.mark.django_db
def test_drop_target_tag_scope_never_aborts_on_leftovers(migrator):
    """module_firmware.0015 must never abort on target rows 0005 left behind:
    a leftover "tag" row with a group becomes a group target (unless that
    group already has one), orphans are deleted, stray refs are cleared."""
    old = migrator.apply_initial_migration(
        [("rollouts", "0005_tags_to_groups"), ("module_firmware", "0014_target_group")]
    ).apps
    seed = _Seed(old)
    group_model = old.get_model("stations", "RolloutGroup")
    g1 = group_model.objects.create(name="g1", slug="g1")
    g2 = group_model.objects.create(name="g2", slug="g2")
    mt = seed.module_type()
    mapped = seed.Target.objects.create(
        module_type=mt, version="1", scope="tag", tag=seed.tag("t1"), group=g1
    )
    seed.Target.objects.create(module_type=mt, version="2", scope="tag", tag=seed.tag("t2"))
    seed.Target.objects.create(module_type=mt, version="3", scope="group")
    kept = seed.Target.objects.create(
        module_type=mt, version="4", scope="group", group=g2, canary_group=g1
    )
    seed.Target.objects.create(
        module_type=mt, version="5", scope="tag", tag=seed.tag("t3"), group=g2
    )
    fleet = seed.Target.objects.create(module_type=mt, version="6", scope="fleet", group=g1)

    new = migrator.apply_tested_migration([("module_firmware", "0015_drop_tag_scope")]).apps

    target_model = new.get_model("module_firmware", "ModuleFirmwareTarget")
    rows = {r.pk: (r.scope, r.group_id, r.canary_group_id) for r in target_model.objects.all()}
    assert rows == {
        mapped.pk: ("group", g1.pk, None),
        kept.pk: ("group", g2.pk, None),
        fleet.pk: ("fleet", None, None),
    }
