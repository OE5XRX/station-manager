from unittest import mock

from apps.images import cosign


def test_verify_blob_identity_passes_regexp_to_cosign():
    with mock.patch("apps.images.cosign.subprocess.run") as run:
        run.return_value = mock.Mock(returncode=0, stderr=b"")
        cosign.verify_blob_identity(b"blob", b"bundle", r"^https://x/main$")
        args = run.call_args[0][0]
        assert "--certificate-identity-regexp" in args
        assert args[args.index("--certificate-identity-regexp") + 1] == r"^https://x/main$"
        assert "--certificate-oidc-issuer" in args
