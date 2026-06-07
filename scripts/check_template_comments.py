#!/usr/bin/env python3
"""Fail-loud check for multi-line ``{# ... #}`` Django template comments.

Django's ``{# ... #}`` comment syntax is **single-line only**. When the
text spans a newline the template engine does not recognise it as a
comment and renders the entire block — including the ``{#`` and ``#}``
markers — as literal text in the page. This has bitten the project at
least twice in production (PR #61, PR #65).

This linter walks all ``*.html`` files under the ``apps/`` and
``templates/`` directories, reports every offender as ``path:line`` with
a snippet, points at the fix (``{% comment %}…{% endcomment %}``), and
exits non-zero so CI catches it before merge.

Run locally:

    python scripts/check_template_comments.py

The script keeps zero dependencies beyond the stdlib so it stays cheap
to run in the lint job.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SEARCH_ROOTS = (REPO_ROOT / "apps", REPO_ROOT / "templates")

# Matches any ``{#`` followed by ``#}`` where the body spans at least one
# newline. ``DOTALL`` makes ``.`` match newlines; the inner
# negative-lookahead ``(?:(?!#\}).)`` keeps the body from swallowing the
# closing marker, so we always pick the shortest valid block.
MULTILINE_COMMENT = re.compile(
    r"\{#((?:(?!#\}).)*?\n(?:(?!#\}).)*?)#\}",
    re.DOTALL,
)


def _iter_template_files() -> list[Path]:
    return [p for root in SEARCH_ROOTS if root.is_dir() for p in root.rglob("*.html")]


def _find_offenders(text: str) -> list[tuple[int, str]]:
    """Return ``(line_no, snippet)`` tuples for every multi-line block."""
    offenders: list[tuple[int, str]] = []
    for match in MULTILINE_COMMENT.finditer(text):
        line_no = text.count("\n", 0, match.start()) + 1
        # Show the opening line of the comment so the operator can find
        # it fast in their editor.
        opening_line = match.group(0).split("\n", 1)[0]
        snippet = opening_line.strip()[:96]
        offenders.append((line_no, snippet))
    return offenders


def main() -> int:
    hits: list[tuple[Path, int, str]] = []
    for path in _iter_template_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
            continue
        for line_no, snippet in _find_offenders(text):
            hits.append((path.relative_to(REPO_ROOT), line_no, snippet))

    if not hits:
        return 0

    print("Multi-line {# … #} Django comments found:", file=sys.stderr)
    for path, line_no, snippet in hits:
        print(f"  {path}:{line_no}  {snippet}…", file=sys.stderr)
    print(
        "\nDjango's {# … #} is single-line only. Multi-line versions render\n"
        "as raw text on the page. Replace each block with:\n"
        "    {% comment %}\n"
        "    your comment here\n"
        "    {% endcomment %}\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
