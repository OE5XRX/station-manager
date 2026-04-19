from django.db import migrations


def seed_singleton(apps, schema_editor):
    RolloutSequence = apps.get_model("rollouts", "RolloutSequence")
    RolloutSequence.objects.get_or_create(pk=1)


def unseed_singleton(apps, schema_editor):
    RolloutSequence = apps.get_model("rollouts", "RolloutSequence")
    RolloutSequence.objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [("rollouts", "0001_initial")]
    operations = [migrations.RunPython(seed_singleton, unseed_singleton)]
