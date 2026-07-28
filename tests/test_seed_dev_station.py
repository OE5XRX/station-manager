import io
import os

import pytest
from django.core.management import call_command
from django.test import override_settings

from apps.api.models import DeviceKey
from apps.stations.models import Station


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_seed_dev_station_is_idempotent(tmp_path):
    key_out = str(tmp_path / "k.pem")
    out1 = io.StringIO()
    call_command("seed_dev_station", "--key-out", key_out, stdout=out1)
    out2 = io.StringIO()
    call_command("seed_dev_station", "--key-out", key_out, stdout=out2)

    assert Station.objects.filter(name="Dev Station").count() == 1
    assert DeviceKey.objects.filter(station__name="Dev Station").count() == 1
    assert "server_url:" in out2.getvalue()
    assert "station_id:" in out2.getvalue()
    assert "ed25519_key_path:" in out2.getvalue()
    assert (os.stat(key_out).st_mode & 0o777) == 0o600


@pytest.mark.django_db
@override_settings(DEBUG=False)
def test_seed_dev_station_refuses_in_prod(tmp_path):
    from django.core.management.base import CommandError
    with pytest.raises(CommandError):
        call_command("seed_dev_station", "--key-out", str(tmp_path / "k.pem"))
