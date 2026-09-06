"""Guard against the packaging bug Session E's QEMU E2E gate caught.

`station_agent/pyproject.toml` uses an EXPLICIT `[tool.setuptools] packages` list. A
subpackage that exists on disk but is missing from that list is silently dropped from
the installed wheel/image — it still imports fine in editable/source dev, so unit tests
pass, but `python -m station_agent selftest audio` fails on-target with
ModuleNotFoundError. `station_agent.audio` was exactly this miss. This test fails fast
(no wheel build) if any on-disk subpackage is not declared.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

_AGENT = Path(__file__).resolve().parent.parent / "station_agent"


def test_every_subpackage_is_declared_in_pyproject():
    pyproject = tomllib.loads((_AGENT / "pyproject.toml").read_text())
    declared = set(pyproject["tool"]["setuptools"]["packages"])

    _ignore = {"tests", "__pycache__", "build", "dist"}
    found = {"station_agent"}
    for init in _AGENT.rglob("__init__.py"):
        rel = init.parent.relative_to(_AGENT)
        parts = rel.parts
        if not parts or _ignore & set(parts) or any(p.endswith(".egg-info") for p in parts):
            continue
        found.add("station_agent." + ".".join(parts))

    missing = found - declared
    assert not missing, (
        f"subpackages present on disk but missing from pyproject "
        f"[tool.setuptools] packages (will be dropped from the installed image): "
        f"{sorted(missing)}"
    )
