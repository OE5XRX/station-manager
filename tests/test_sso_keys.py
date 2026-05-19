import pytest
from cryptography.hazmat.primitives import serialization
from django.core.management import call_command


@pytest.mark.django_db
def test_setup_oidc_keys_creates_a_valid_rsa_2048_private_key(tmp_path):
    """First run: writes a fresh 2048-bit RSA PEM at the target path."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))

    assert target.exists()
    pem = target.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    assert key.key_size == 2048


@pytest.mark.django_db
def test_setup_oidc_keys_is_idempotent(tmp_path):
    """Second run with an existing key leaves it untouched."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    original = target.read_bytes()
    call_command("setup_oidc_keys", path=str(target))
    assert target.read_bytes() == original


@pytest.mark.django_db
def test_setup_oidc_keys_force_overwrites(tmp_path):
    """With --force, an existing key is regenerated."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    original = target.read_bytes()
    call_command("setup_oidc_keys", path=str(target), force=True)
    assert target.read_bytes() != original
