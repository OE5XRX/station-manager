"""collectstatic (WhiteNoise CompressedManifestStaticFilesStorage) fails the image
build if a static file references a source map that is not shipped."""

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
_SOURCEMAP = re.compile(r"sourceMappingURL=([^\s*]+)")


def test_referenced_sourcemaps_exist():
    missing = []
    for path in list(STATIC.rglob("*.js")) + list(STATIC.rglob("*.css")):
        for ref in _SOURCEMAP.findall(path.read_text(errors="ignore")):
            if ref.startswith("data:"):
                continue
            if not (path.parent / ref).exists():
                missing.append(f"{path.relative_to(STATIC)} -> {ref}")
    assert not missing, missing
