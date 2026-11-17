"""Guard: Alpine expressions must not start with a statement keyword.

Alpine wraps an expression in a function body only when it starts with
``let``/``const``/``if (``; anything else is compiled as an expression, so a
leading ``var``/``try``/``for``/... throws "Unexpected token" at runtime (the
TX-route select in ``_audio_panel.html`` broke this way with ``var``). Move
such logic into a component method instead.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# @event / x-on:event / x-init / x-effect / x-data attribute values.
ALPINE_ATTR = re.compile(
    r"(?:@[\w.:-]+|x-on:[\w.:-]+|x-init|x-effect|x-data)\s*=\s*([\"'])\s*"
    r"(?:var|try|for|while|do|switch|throw|return)\b",
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
    assert not offenders, "Alpine expression starts with a statement (use a method): " + ", ".join(
        offenders
    )


def test_guard_detects_the_original_bug():
    assert ALPINE_ATTR.search('<select @change="\n  var v = $event.target.value;">')
    assert not ALPINE_ATTR.search('<select @change="onTxRouteChange($event.target.value)">')


def test_guard_covers_other_statements_and_quotes():
    for bad in (
        "<a @click='var x = 1'>",
        '<a x-on:click="try { go() } catch (e) {}">',
        '<a x-init="for (const s of xs) add(s)">',
        '<a @keydown.enter="return false">',
    ):
        assert ALPINE_ATTR.search(bad), bad
    for good in (
        '<a @click="let x = 1; go(x)">',
        '<a x-init="if (ready) start()">',
        '<a @click="variant = 1">',
        '<a @click="format(x)">',
        '<a @click="doIt()">',
    ):
        assert not ALPINE_ATTR.search(good), good
