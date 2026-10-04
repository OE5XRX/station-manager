import math

import pytest

from apps.stations import tx_audio
from apps.stations.forms import StationForm


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, -12.0),
        (-12.0, -12.0),
        (-6.5, -6.5),
        (-30.0, -24.0),  # below range -> clamp to the quiet end
        (0.0, -3.0),  # above range -> clamp (never splatter)
        (5, -3.0),
        (float("nan"), -12.0),
        (float("inf"), -12.0),
        ("loud", -12.0),
        (True, -12.0),  # bool is not a calibration number
    ],
)
def test_effective_ceiling_clamps_and_defaults(raw, expected):
    assert tx_audio.effective_ceiling_dbfs(raw) == expected


def test_default_is_conservative_and_in_range():
    # Brief's "<= midrange" assertion contradicts its own constants (mid = -13.5 < -12);
    # assert what it means: inside the range and well below the loud end.
    assert tx_audio.CEILING_MIN_DBFS <= tx_audio.CEILING_DEFAULT_DBFS
    assert tx_audio.CEILING_DEFAULT_DBFS <= tx_audio.CEILING_MAX_DBFS - 6
    assert math.isfinite(tx_audio.CEILING_DEFAULT_DBFS)


@pytest.mark.django_db
def test_station_field_nullable_default_none(station_factory):
    s = station_factory()
    assert s.tx_audio_ceiling_dbfs is None


@pytest.mark.django_db
def test_form_exposes_field_only_to_internal(operator_user, member_user):
    assert "tx_audio_ceiling_dbfs" in StationForm(user=operator_user).fields
    assert "tx_audio_ceiling_dbfs" not in StationForm(user=member_user).fields
    assert "tx_audio_ceiling_dbfs" not in StationForm().fields  # no user -> safe default


@pytest.mark.django_db
def test_form_rejects_out_of_range_for_staff(operator_user):
    f = StationForm(
        data={"name": "S", "callsign": "OE5XRX", "tx_audio_ceiling_dbfs": "-40"},
        user=operator_user,
    )
    assert not f.is_valid()
    assert "tx_audio_ceiling_dbfs" in f.errors
