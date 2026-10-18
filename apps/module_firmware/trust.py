"""Trust root for firmware signer identity: which GitHub owners may supply firmware.

Single source for the "is this module type import-ready" rule, shared by the
type form, the type list, the release-list setup hints, the import views and
the importer itself.
"""

import re

from django.conf import settings
from django.utils.translation import gettext as _

# GitHub ``owner/repo`` shape (exactly one slash, non-empty owner and repo).
# ``\Z`` not ``$``: ``$`` also matches before a trailing newline, and
# RegexValidator uses ``search`` — the anchors must be strict on both ends.
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+\Z"
_REPO_RE = re.compile(REPO_PATTERN)

# Release tag: interpolated into GitHub URLs and stored in a 64-char column.
_TAG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def trusted_owners():
    return [o for o in getattr(settings, "MODULE_FIRMWARE_TRUSTED_REPO_OWNERS", []) if o]


def is_valid_repo(repo):
    return bool(repo) and _REPO_RE.match(repo) is not None


def is_valid_tag(tag):
    """Safe release tag: ASCII ``[A-Za-z0-9][A-Za-z0-9._-]{0,63}``, no ``..``."""
    return isinstance(tag, str) and _TAG_RE.fullmatch(tag) is not None and ".." not in tag


def is_trusted_repo(repo):
    # Case-SENSITIVE on purpose: the cosign identity regex built from the repo
    # is case-sensitive, so a case-folded match here would report a type as
    # import-ready while every signature verification fails.
    if not is_valid_repo(repo):
        return False
    return repo.partition("/")[0] in set(trusted_owners())


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
