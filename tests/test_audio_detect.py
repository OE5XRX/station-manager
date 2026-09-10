from station_agent.audio.detect import audio_path_present


class _Cfg:
    audio_sysfs_sound = "/sys/class/sound"


class _Backend:
    def __init__(self, slots):
        self._slots = slots

    def list_audio_slots(self):
        return self._slots


class _RaisingBackend:
    def list_audio_slots(self):
        raise OSError("no sysfs")


def test_audio_present_when_backend_lists_slots():
    assert audio_path_present(_Cfg(), backend=_Backend([3])) is True


def test_audio_absent_when_no_slots():
    assert audio_path_present(_Cfg(), backend=_Backend([])) is False


def test_audio_detect_fails_closed_on_error():
    assert audio_path_present(_Cfg(), backend=_RaisingBackend()) is False
