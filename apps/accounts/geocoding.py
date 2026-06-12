"""Geocoding + Maidenhead locator helpers (Sub-Spec 1a Foundation).

`geocode_address` resolves a postal address to (lat, lon) via Nominatim/OSM
(added in Task 10).
`lat_lon_to_locator` computes the Maidenhead 6-char grid locator from
(lat, lon).

Spec: docs/superpowers/specs/2026-06-12-user-domain-1a-foundation-design.md
"""


def lat_lon_to_locator(lat, lon, precision: int = 6) -> str:
    """Maidenhead-Locator aus (lat, lon).

    precision=6 → 6-Zeichen-Locator (z.B. 'JN78DH').
    precision=4 → 4-Zeichen Grid-Square (z.B. 'JN78').

    Algorithmus:
      1. Verschiebung: lon += 180, lat += 90 (alles wird positiv).
      2. Fields (1. Letter-Pair): 18×18 Grid à 20° lon / 10° lat (A-R).
      3. Squares (2. Digit-Pair): 10×10 Grid à 2° lon / 1° lat (0-9).
      4. Subsquares (3. Letter-Pair): 24×24 Grid à 5' lon / 2.5' lat (A-X).

    Akzeptiert float, int, Decimal — wird intern zu float konvertiert.
    """
    lat_f = float(lat) + 90.0
    lon_f = float(lon) + 180.0
    a_ord = ord("A")
    lon_field, lon_rest = divmod(lon_f, 20.0)
    lat_field, lat_rest = divmod(lat_f, 10.0)
    out = chr(a_ord + int(lon_field)) + chr(a_ord + int(lat_field))
    lon_sq, lon_rest = divmod(lon_rest, 2.0)
    lat_sq, lat_rest = divmod(lat_rest, 1.0)
    out += str(int(lon_sq)) + str(int(lat_sq))
    if precision >= 6:
        # Per 2° lon → 24 subsquares (5'/60° per subsquare), so multiply by 12
        # to map [0, 2) to [0, 24).
        lon_sub = int(lon_rest * 12)
        # Per 1° lat → 24 subsquares (2.5'/60° per subsquare), so multiply by 24
        # to map [0, 1) to [0, 24).
        lat_sub = int(lat_rest * 24)
        out += chr(a_ord + lon_sub) + chr(a_ord + lat_sub)
    return out
