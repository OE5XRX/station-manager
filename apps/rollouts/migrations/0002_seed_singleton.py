from django.db import migrations


def seed_singleton(apps, schema_editor):
    """Seed one RolloutSequence row if none exists.

    Deliberately does NOT pass an explicit pk. On Postgres, explicit-pk
    inserts don't advance the underlying sequence, which would cause a
    later pk-free create (Django admin, factory, etc.) to collide on
    whichever id we would have hard-coded here.
    """
    RolloutSequence = apps.get_model("rollouts", "RolloutSequence")
    if not RolloutSequence.objects.exists():
        RolloutSequence.objects.create()


def unseed_singleton(apps, schema_editor):
    RolloutSequence = apps.get_model("rollouts", "RolloutSequence")
    RolloutSequence.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("rollouts", "0001_initial")]
    operations = [migrations.RunPython(seed_singleton, unseed_singleton)]
