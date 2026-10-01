import pytest

from station_agent.bootinfo import compute_reboot_reason

BASE = dict(pstore_crash=False, ota_rollback=False, watchdog=False,
            undervoltage_occurred=False, clean_marker=False)


def ev(**kw):
    return {**BASE, **kw}


@pytest.mark.parametrize("evidence,expected", [
    (ev(pstore_crash=True), "crash"),
    (ev(pstore_crash=True, clean_marker=True), "crash"),       # crash wins
    (ev(ota_rollback=True), "ota_rollback"),
    (ev(watchdog=True), "watchdog"),
    (ev(undervoltage_occurred=True), "undervoltage"),
    (ev(undervoltage_occurred=True, clean_marker=True), "clean"),  # clean shutdown wins over UV-occurred
    (ev(clean_marker=True), "clean"),
    (ev(), "unknown"),
])
def test_compute_reboot_reason(evidence, expected):
    assert compute_reboot_reason(evidence) == expected
