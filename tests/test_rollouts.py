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


@pytest.mark.django_db(transaction=True)
class TestSingletonSeed:
    def test_exactly_one_sequence_exists_after_migrations(self):
        # transaction=True → each test starts from the migrated DB state,
        # so the singleton seeded by 0002_seed_singleton is present.
        assert RolloutSequence.objects.count() == 1
        assert RolloutSequence.objects.filter(pk=1).exists()

    def test_current_sequence_helper_returns_the_singleton(self):
        from apps.rollouts.models import current_sequence

        seq1 = current_sequence()
        seq2 = current_sequence()
        assert seq1 == seq2
        assert RolloutSequence.objects.count() == 1


@pytest.mark.django_db
class TestGrouping:
    def test_first_matching_tag_wins(self, make_station_tag):
        from apps.rollouts.grouping import group_stations_by_sequence
        from apps.rollouts.models import RolloutSequenceEntry, current_sequence
        from apps.stations.models import Station

        t_test = make_station_tag("test")
        t_easy = make_station_tag("easy")
        seq = current_sequence()
        seq.entries.all().delete()
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_test, position=0)
        RolloutSequenceEntry.objects.create(sequence=seq, tag=t_easy, position=1)

        s = Station.objects.create(name="S1")
        s.tags.add(t_test, t_easy)

        grouped = group_stations_by_sequence([s])
        # s must appear ONLY in the 'test' bucket, not in 'easy' too.
        assert grouped["test"] == [s]
        assert grouped["easy"] == []

    def test_unassigned_bucket(self):
        from apps.rollouts.grouping import group_stations_by_sequence
        from apps.rollouts.models import current_sequence
        from apps.stations.models import Station

        seq = current_sequence()
        seq.entries.all().delete()
        s = Station.objects.create(name="S-none")
        grouped = group_stations_by_sequence([s])
        assert grouped["__unassigned__"] == [s]
