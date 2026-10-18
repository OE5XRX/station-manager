"""Trust root for firmware signer identity: which GitHub owners may supply firmware."""

from django.conf import settings


def trusted_owners():
    return [o for o in getattr(settings, "MODULE_FIRMWARE_TRUSTED_REPO_OWNERS", []) if o]


def is_trusted_repo(repo):
    owner = (repo or "").partition("/")[0].lower()
    return bool(owner) and owner in {o.lower() for o in trusted_owners()}
