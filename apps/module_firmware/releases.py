from __future__ import annotations
from dataclasses import dataclass

_SIGNED_SUFFIX = ".signed.bin"


@dataclass(frozen=True)
class VariantAsset:
    variant: str
    signed_name: str
    bundle_name: str


def parse_variant_assets(asset_names: set[str], prefix: str) -> list[VariantAsset]:
    """From a release's asset names, extract (variant, signed, bundle) tuples.

    Matches ``{prefix}[-{variant}].signed.bin`` where a matching
    ``.bundle`` sibling exists. A bare ``{prefix}.signed.bin`` yields
    variant "". A prefix that is only a substring of a longer token
    (e.g. ``fm-sa818x-...``) is rejected — the char after the prefix
    must be '-' or the suffix itself.
    """
    out: list[VariantAsset] = []
    for name in sorted(asset_names):
        if not (name.startswith(prefix) and name.endswith(_SIGNED_SUFFIX)):
            continue
        middle = name[len(prefix):-len(_SIGNED_SUFFIX)]
        if middle == "":
            variant = ""
        elif middle.startswith("-"):
            variant = middle[1:]
            if not variant:
                continue
        else:
            continue  # prefix is a proper substring of a longer token
        bundle_name = name + ".bundle"
        if bundle_name not in asset_names:
            continue
        out.append(VariantAsset(variant=variant, signed_name=name, bundle_name=bundle_name))
    return out


def parse_sha256sums(text: str) -> dict[str, str]:
    """Parse ``<hex>  <filename>`` lines into {filename: hex}."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            result[parts[-1]] = parts[0]
    return result
