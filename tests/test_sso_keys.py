from cryptography.hazmat.primitives import serialization
from django.core.management import call_command


def test_setup_oidc_keys_creates_a_valid_rsa_2048_private_key(tmp_path):
    """First run: writes a fresh 2048-bit RSA PEM at the target path."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))

    assert target.exists()
    pem = target.read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    assert key.key_size == 2048


def test_setup_oidc_keys_is_idempotent(tmp_path):
    """Second run with an existing key leaves it untouched."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    original = target.read_bytes()
    call_command("setup_oidc_keys", path=str(target))
    assert target.read_bytes() == original


def test_setup_oidc_keys_force_overwrites(tmp_path):
    """With --force, an existing key is regenerated."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    original = target.read_bytes()
    call_command("setup_oidc_keys", path=str(target), force=True)
    assert target.read_bytes() != original


def test_setup_oidc_keys_file_is_mode_0600(tmp_path):
    """The private key must NEVER be readable beyond the owner."""
    target = tmp_path / "private.pem"
    call_command("setup_oidc_keys", path=str(target))
    assert target.stat().st_mode & 0o777 == 0o600


def test_setup_oidc_keys_uses_settings_default_when_no_path_given(tmp_path, settings):
    """No --path argument → command honors settings.OIDC_RSA_KEY_PATH."""
    target = tmp_path / "from-settings.pem"
    settings.OIDC_RSA_KEY_PATH = str(target)
    call_command("setup_oidc_keys")
    assert target.exists()
