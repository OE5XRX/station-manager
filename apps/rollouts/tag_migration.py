"""Data logic for rollouts.0005_tags_to_groups: functional tags → RolloutGroups.

A tag is *functional* if the rollout machinery uses it (sequence entry,
deployment target, module-firmware target/canary). Each functional tag
gets a RolloutGroup; references are remapped and every station carrying
functional tags is placed in exactly one group. Label-only tags are
left alone, and no tag or station-tag assignment is modified.

Operates on a historical app registry (``apps``) only — no imports of
app models — and never raises on dirty data: missing references are
skipped silently.
"""

import logging
import math

from django.db import transaction

logger = logging.getLogger(__name__)

_NAME_MAX = 100  # RolloutGroup.name max_length


def _fit(base: str, suffix: str = "") -> str:
    return base[: _NAME_MAX - len(suffix)] + suffix


def _group_for_tag(group_model, tag):
    """Reuse the group with the tag's slug, else create one with a free name."""
    existing = group_model.objects.filter(slug=tag.slug).first()
    if existing is not None:
        return existing
    taken = set(group_model.objects.values_list("name", flat=True))
    name = _fit(tag.name)
    if name in taken:
        base = f"{tag.name} ({tag.slug})"
        name = _fit(base)
        n = 2
        while name in taken:
            name = _fit(base, f" {n}")
            n += 1
    return group_model.objects.create(
        name=name,
        slug=tag.slug,
        channel="release",
        description=tag.description,
    )


def migrate_tags_to_groups(apps, schema_editor=None) -> None:
    tag_model = apps.get_model("stations", "StationTag")
    group_model = apps.get_model("stations", "RolloutGroup")
    station_model = apps.get_model("stations", "Station")
    audit_model = apps.get_model("stations", "StationAuditLog")
    entry_model = apps.get_model("rollouts", "RolloutSequenceEntry")
    deployment_model = apps.get_model("deployments", "Deployment")
    target_model = apps.get_model("module_firmware", "ModuleFirmwareTarget")

    # 1. Functional tags (+ front-most sequence position per tag).
    position_by_tag: dict[int, int] = {}
    for tag_id, position in entry_model.objects.values_list("tag_id", "position"):
        if tag_id is not None:
            position_by_tag[tag_id] = min(position, position_by_tag.get(tag_id, position))
    functional_ids = set(position_by_tag)
    functional_ids.update(
        deployment_model.objects.filter(target_tag__isnull=False).values_list(
            "target_tag_id", flat=True
        )
    )
    mf_tag_ids = set(
        target_model.objects.filter(tag__isnull=False).values_list("tag_id", flat=True)
    )
    mf_tag_ids.update(
        target_model.objects.filter(canary_tag__isnull=False).values_list(
            "canary_tag_id", flat=True
        )
    )
    functional_ids.update(mf_tag_ids)

    # 2. One group per (still existing) functional tag.
    tags = {t.pk: t for t in tag_model.objects.filter(pk__in=functional_ids).order_by("name")}
    group_by_tag = {tag_id: _group_for_tag(group_model, tag) for tag_id, tag in tags.items()}

    # 3. Remap references.
    for tag_id, group in group_by_tag.items():
        entry_model.objects.filter(tag_id=tag_id).update(group_id=group.pk)
    for dep_id, tag_id in deployment_model.objects.filter(target_type="tag").values_list(
        "pk", "target_tag_id"
    ):
        group = group_by_tag.get(tag_id)
        deployment_model.objects.filter(pk=dep_id).update(
            target_type="group", target_group_id=group.pk if group else None
        )
    for target_id, scope, tag_id, canary_tag_id in target_model.objects.values_list(
        "pk", "scope", "tag_id", "canary_tag_id"
    ):
        changes = {}
        if scope == "tag":
            group = group_by_tag.get(tag_id)
            changes.update(scope="group", group_id=group.pk if group else None)
        canary = group_by_tag.get(canary_tag_id)
        if canary is not None:
            changes["canary_group_id"] = canary.pk
        if changes:
            target_model.objects.filter(pk=target_id).update(**changes)

    # 4. Place each ungrouped station carrying functional tags.
    through = station_model.tags.through
    station_tags: dict[int, list[int]] = {}
    for station_id, tag_id in through.objects.filter(
        stationtag_id__in=group_by_tag,
        station__rollout_group__isnull=True,
    ).values_list("station_id", "stationtag_id"):
        station_tags.setdefault(station_id, []).append(tag_id)

    for station_id, tag_ids in station_tags.items():
        ranked = sorted(
            tag_ids, key=lambda tid: (position_by_tag.get(tid, math.inf), tags[tid].name)
        )
        group = group_by_tag[ranked[0]]
        station_model.objects.filter(pk=station_id).update(rollout_group_id=group.pk)
        if len(ranked) < 2:
            continue
        names = sorted(tags[tid].name for tid in tag_ids)
        basis = (
            "front-most in sequence"
            if ranked[0] in position_by_tag
            else "no tag in sequence; first by name"
        )
        dropped = sorted(tags[tid].name for tid in ranked[1:] if tid in mf_tag_ids)
        carried = f" — not carried over: {dropped}" if dropped else ""
        try:
            with transaction.atomic():
                audit_model.objects.create(
                    station_id=station_id,
                    event_type="rollout_group_changed",
                    message=(
                        f"Migration: functional tags {names} → rollout group "
                        f"'{group.name}' ({basis}){carried}"
                    ),
                    user=None,
                )
        except Exception:
            # Audit is best-effort here: a failing insert must never abort
            # the migration (the group assignment itself already happened).
            logger.exception("tags_to_groups: audit insert failed for station %s", station_id)
