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


def test_symbol_sheet_attributes_are_read_the_way_the_templates_write_them():
    """Regression: the pins had no symbols because JS read ``dataset.symbols0``.

    ``data-symbols-0`` is ``dataset["symbols-0"]`` (the hyphen before a digit is kept),
    so the sprite URL was ``url(undefined)`` and only the callsign chips rendered.
    """
    assert not re.search(r"dataset\.symbols\d", MAP_JS)
    assert 'getAttribute("data-symbols-" + i)' in MAP_JS
    for tpl in (
        "apps/passive/templates/passive/aprs.html",
        "apps/passive/templates/passive/_station_aprs_tab.html",
    ):
        html = (REPO_ROOT / tpl).read_text()
        for i in range(3):
            assert f'data-symbols-{i}="{{% static ' in html, (tpl, i)
