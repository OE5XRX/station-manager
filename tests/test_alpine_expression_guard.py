"""Guard: Alpine expressions must not start with a ``var`` statement.

Alpine evaluates an expression as ``(async () => { <expr> })`` only when it
starts with ``let``/``const``/``if``/etc.; a leading ``var`` is compiled as an
expression and throws "Unexpected token 'var'" at runtime (the TX-route select
in ``_audio_panel.html`` broke this way). Move such logic into a component
method instead.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# @event / x-on:event / x-init / x-effect / x-data attribute values.
ALPINE_ATTR = re.compile(
    r'(?:@[\w.:-]+|x-on:[\w.:-]+|x-init|x-effect|x-data)\s*=\s*"\s*var\s',
    re.S,
)


def _templates():
    for base in (REPO_ROOT / "apps", REPO_ROOT / "templates"):
        if base.exists():
            yield from base.rglob("*.html")


def test_no_alpine_expression_starts_with_var():
    offenders = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for m in ALPINE_ATTR.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{line}")
    assert not offenders, "Alpine expression starts with `var` (use a method): " + ", ".join(
        offenders
    )


def test_guard_detects_the_original_bug():
    assert ALPINE_ATTR.search('<select @change="\n  var v = $event.target.value;">')
    assert not ALPINE_ATTR.search('<select @change="onTxRouteChange($event.target.value)">')
