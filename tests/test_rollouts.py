import pytest
from django.db import IntegrityError

from apps.rollouts.models import RolloutSequence, RolloutSequenceEntry
from apps.stations.models import StationTag


@pytest.mark.django_db
class TestRolloutSequence:
    def test_entries_are_ordered_by_position(self):
        seq = RolloutSequence.objects.create()
        tag_a = StationTag.objects.create(name="alpha", slug="alpha")
        tag_b = StationTag.objects.create(name="beta", slug="beta")
        tag_c = StationTag.objects.create(name="gamma", slug="gamma")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_b, position=1)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_a, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag_c, position=2)

        ordered = list(seq.entries.values_list("tag__name", flat=True))
        assert ordered == ["alpha", "beta", "gamma"]

    def test_tag_unique_per_sequence(self):
        seq = RolloutSequence.objects.create()
        tag = StationTag.objects.create(name="t1", slug="t1")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=0)
        with pytest.raises(IntegrityError):
            RolloutSequenceEntry.objects.create(sequence=seq, tag=tag, position=1)

    def test_position_unique_per_sequence(self):
        seq = RolloutSequence.objects.create()
        t1 = StationTag.objects.create(name="t1", slug="t1")
        t2 = StationTag.objects.create(name="t2", slug="t2")

        RolloutSequenceEntry.objects.create(sequence=seq, tag=t1, position=0)
        with pytest.raises(IntegrityError):
            RolloutSequenceEntry.objects.create(sequence=seq, tag=t2, position=0)
