from django.db import migrations, models


def leftover_tag_rows_to_group(apps, schema_editor):
    """Safety net: rollouts.0005 already remapped tag-scoped targets. Never
    abort: a leftover "tag" row with a group becomes a group target; one
    without a group (or a group target missing its group) is an orphan that
    the new check constraints would reject, so it is deleted. A leftover that
    would duplicate an existing group target is dropped (the existing group
    target wins), and stray group/canary_group refs the new constraints forbid
    are cleared."""
    target = apps.get_model("module_firmware", "ModuleFirmwareTarget")
    taken = set(
        target.objects.filter(scope="group", group__isnull=False).values_list(
            "module_type_id", "group_id"
        )
    )
    for row in target.objects.filter(scope="tag", group__isnull=False).order_by(
        "-updated_at", "-pk"
    ):
        key = (row.module_type_id, row.group_id)
        if key in taken:
            row.delete()
            continue
        taken.add(key)
        target.objects.filter(pk=row.pk).update(scope="group", station=None)
    target.objects.filter(scope="tag").delete()
    target.objects.filter(scope="group", group__isnull=True).delete()
    target.objects.filter(scope="group").exclude(station=None).update(station=None)
    target.objects.exclude(scope="group").exclude(group=None).update(group=None)
    target.objects.exclude(scope="fleet").exclude(canary_group=None).update(canary_group=None)


class Migration(migrations.Migration):
    dependencies = [
        ("module_firmware", "0014_target_group"),
        ("rollouts", "0005_tags_to_groups"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="modulefirmwaretarget", name="uniq_tag_target_per_type_tag"
        ),
        migrations.RemoveConstraint(
            model_name="modulefirmwaretarget", name="target_fleet_has_no_ref"
        ),
        migrations.RemoveConstraint(
            model_name="modulefirmwaretarget", name="target_tag_has_tag_only"
        ),
        migrations.RemoveConstraint(
            model_name="modulefirmwaretarget", name="target_station_has_station_only"
        ),
        migrations.RemoveConstraint(
            model_name="modulefirmwaretarget", name="canary_tag_only_on_fleet"
        ),
        migrations.RunPython(leftover_tag_rows_to_group, migrations.RunPython.noop),
        migrations.RemoveField(model_name="modulefirmwaretarget", name="tag"),
        migrations.RemoveField(model_name="modulefirmwaretarget", name="canary_tag"),
        migrations.AlterField(
            model_name="modulefirmwaretarget",
            name="scope",
            field=models.CharField(
                choices=[
                    ("fleet", "Fleet default"),
                    ("group", "Group override"),
                    ("station", "Station override"),
                ],
                default="fleet",
                max_length=16,
                verbose_name="scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulefirmwaretarget",
            constraint=models.UniqueConstraint(
                condition=models.Q(scope="group"),
                fields=("module_type", "group"),
                name="uniq_group_target_per_type_group",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulefirmwaretarget",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ~models.Q(scope="fleet"),
                    models.Q(group__isnull=True, station__isnull=True),
                    _connector="OR",
                ),
                name="target_fleet_has_no_ref",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulefirmwaretarget",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ~models.Q(scope="group"),
                    models.Q(group__isnull=False, station__isnull=True),
                    _connector="OR",
                ),
                name="target_group_has_group_only",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulefirmwaretarget",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ~models.Q(scope="station"),
                    models.Q(("station__isnull", False), ("group__isnull", True)),
                    _connector="OR",
                ),
                name="target_station_has_station_only",
            ),
        ),
        migrations.AddConstraint(
            model_name="modulefirmwaretarget",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scope", "fleet"), ("canary_group__isnull", True), _connector="OR"
                ),
                name="canary_group_only_on_fleet",
            ),
        ),
    ]
