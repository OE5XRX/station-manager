"""Regression: HTTP 500 when adding the second entry to the rollout sequence.

Root cause: ``SequenceAddView`` computed the next position as
``(Max("position") or -1) + 1``. With exactly one entry its position is 0,
``0 or -1`` evaluates to -1, so the new entry was also given position 0 and
the ``uniq_position_per_sequence`` constraint raised an IntegrityError (500).
The API path (``apps.rollouts.services.add_entry``) already used an explicit
``None`` check; the UI view now delegates to that service.
"""

import pytest
from django.urls import reverse

from apps.rollouts.models import RolloutSequenceEntry, current_sequence


@pytest.mark.django_db
@pytest.mark.parametrize("existing", [0, 1, 2])
def test_admin_can_append_to_sequence(client, admin_user, make_station_tag, existing):
    client.raise_request_exception = True
    seq = current_sequence()
    seq.entries.all().delete()
    for i in range(existing):
        RolloutSequenceEntry.objects.create(
            sequence=seq, tag=make_station_tag(f"pre{i}"), position=i
        )
    tag = make_station_tag("new")
    client.force_login(admin_user)

    response = client.post(reverse("rollouts:sequence_add"), {"tag": tag.pk})

    assert response.status_code == 302
    assert list(seq.entries.order_by("position").values_list("position", flat=True)) == list(
        range(existing + 1)
    )
    assert seq.entries.get(tag=tag).position == existing


@pytest.mark.django_db
@pytest.mark.parametrize("who", ["staff_user", "member_user"])
def test_non_admin_gets_403_not_500(request, client, make_station_tag, who):
    seq = current_sequence()
    seq.entries.all().delete()
    RolloutSequenceEntry.objects.create(sequence=seq, tag=make_station_tag("pre"), position=0)
    tag = make_station_tag("new")
    client.force_login(request.getfixturevalue(who))

    response = client.post(reverse("rollouts:sequence_add"), {"tag": tag.pk})

    assert response.status_code == 403
    assert seq.entries.count() == 1
