"""OSM tile policy: the tile layer must send a cross-origin Referer.

Django's default Referrer-Policy is ``same-origin``, so without a per-layer
override the browser sends no Referer to tile.openstreetmap.org and OSM answers
403 "Access blocked". The global policy stays untouched.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_JS = (REPO_ROOT / "static" / "js" / "aprs-map.js").read_text()
LEAFLET = (REPO_ROOT / "static" / "vendor" / "leaflet" / "leaflet.js").read_text()


def test_vendored_leaflet_supports_referrer_policy():
    assert "referrerPolicy" in LEAFLET


def test_tile_layer_sets_referrer_policy():
    match = re.search(r"L\.tileLayer\(.*?\)\.addTo\(map\)", MAP_JS, re.S)
    assert match, "tile layer call not found"
    assert 'referrerPolicy: "strict-origin-when-cross-origin"' in match.group(0)
    assert "OpenStreetMap" in match.group(0), "attribution must stay"


def test_symbol_sheet_attributes_match_the_templates():
    """Regression: pins had no symbols because JS read ``dataset.symbols0``.

    ``data-symbols-0`` is ``dataset["symbols-0"]`` (no camelCase before a digit), so the
    sprite URL was ``url(undefined)`` and only the callsign chips rendered.
    """
    assert not re.search(r"dataset\.symbols\d", MAP_JS)
    keys = re.findall(r"root\.dataset\.(symbolsSheet[A-C])", MAP_JS)
    assert keys == ["symbolsSheetA", "symbolsSheetB", "symbolsSheetC"]
    for tpl in (
        "apps/passive/templates/passive/aprs.html",
        "apps/passive/templates/passive/_station_aprs_tab.html",
    ):
        html = (REPO_ROOT / tpl).read_text()
        for letter in "abc":
            assert f'data-symbols-sheet-{letter}="{{% static ' in html, (tpl, letter)
        assert not re.search(r"data-symbols-\d", html)


def test_symbol_node_falls_back_when_a_sheet_url_is_missing():
    assert "!SHEETS[info.sheet]" in MAP_JS
