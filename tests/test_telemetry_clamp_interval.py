import pytest

from apps.control import telemetry_subscriptions as subs


@pytest.mark.parametrize(
    "raw, expected",
    [
        (float("nan"), subs.INTERVAL_DEFAULT_MS),
        (True, subs.INTERVAL_DEFAULT_MS),
        (False, subs.INTERVAL_DEFAULT_MS),
        (0, subs.INTERVAL_DEFAULT_MS),
        (-5, subs.INTERVAL_DEFAULT_MS),
        (-float("inf"), subs.INTERVAL_DEFAULT_MS),
        ("500", subs.INTERVAL_DEFAULT_MS),
        (None, subs.INTERVAL_DEFAULT_MS),
        ([500], subs.INTERVAL_DEFAULT_MS),
        (1, subs.INTERVAL_MIN_MS),
        (10**12, subs.INTERVAL_MAX_MS),
        (float("inf"), subs.INTERVAL_MAX_MS),
        (1000, 1000),
        (1000.9, 1000),
    ],
)
def test_clamp_interval(raw, expected):
    assert subs.clamp_interval(raw) == expected
