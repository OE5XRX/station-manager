"""Trust root for firmware signer identity: which GitHub owners may supply firmware.

Single source for the "is this module type import-ready" rule, shared by the
type form, the type list, the release-list setup hints, the import views and
the importer itself.
"""

import re

from django.conf import settings
from django.utils.translation import gettext as _

# GitHub ``owner/repo`` shape (exactly one slash, non-empty owner and repo).
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$"
_REPO_RE = re.compile(REPO_PATTERN)


def trusted_owners():
    return [o for o in getattr(settings, "MODULE_FIRMWARE_TRUSTED_REPO_OWNERS", []) if o]


def is_valid_repo(repo):
    return bool(repo) and _REPO_RE.match(repo) is not None


def is_trusted_repo(repo):
    if not is_valid_repo(repo):
        return False
    owner = repo.partition("/")[0].lower()
    return owner in {o.lower() for o in trusted_owners()}


def import_blockers(module_type):
    """Human-readable reasons why ``module_type`` is not import-ready ([] = ready)."""
    meta = type(module_type)._meta
    reasons = []
    if not module_type.firmware_repo:
        reasons.append(str(meta.get_field("firmware_repo").verbose_name))
    if not module_type.release_asset_prefix:
        reasons.append(str(meta.get_field("release_asset_prefix").verbose_name))
    if module_type.firmware_repo:
        if not is_valid_repo(module_type.firmware_repo):
            reasons.append(_("repo is not in owner/repo form"))
        elif not is_trusted_repo(module_type.firmware_repo):
            reasons.append(_("untrusted repo owner"))
    return reasons
