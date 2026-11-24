#!/usr/bin/env bash
# German-translation tooling: make | compile | check
#   make     regenerate locale/de/LC_MESSAGES/{django,djangojs}.po from the sources
#   compile  compile the .po files to .mo (needs gettext's msgfmt)
#   check    make, fail on untranslated/fuzzy entries, then compile (used by CI)
# Needs DJANGO_SECRET_KEY (and optionally DJANGO_SETTINGS_MODULE) in the env.
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python}"

IGNORES=(.venv/* vendor/* staticfiles/* tests/* station_agent/* docs/* scripts/* node_modules/*)
MK_IGN=()
CM_IGN=()
for p in "${IGNORES[@]}"; do
  MK_IGN+=(-i "$p")
  CM_IGN+=(--ignore "$p")
done

PO_FILES=(locale/de/LC_MESSAGES/django.po locale/de/LC_MESSAGES/djangojs.po)

do_make() {
  mkdir -p locale
  "$PYTHON" manage.py makemessages -l de --no-obsolete "${MK_IGN[@]}"
  "$PYTHON" manage.py makemessages -d djangojs -l de --no-obsolete "${MK_IGN[@]}"
}

do_compile() {
  mkdir -p locale
  "$PYTHON" manage.py compilemessages -l de "${CM_IGN[@]}"
}

# Print the msgid/continuation lines of a msgattrib result (empty if no
# entries). The PO header is never untranslated, but strip it defensively.
entries() {
  local text
  text="$(cat)"
  # No "msgid" line at all -> nothing to report.
  grep -q '^msgid ' <<<"$text" || return 0
  grep -E '^(msgid |msgid_plural |")' <<<"$text" || true
}

do_check() {
  do_make
  local failed=0 po out
  for po in "${PO_FILES[@]}"; do
    # djangojs.po is not generated while the JS has no gettext calls.
    [ -f "$po" ] || continue
    out="$(msgattrib --untranslated --no-obsolete --no-wrap "$po" | entries)"
    if [ -n "$out" ]; then
      echo "ERROR: untranslated entries in $po:" >&2
      echo "$out" >&2
      failed=1
    fi
    out="$(msgattrib --only-fuzzy --no-wrap "$po" | entries)"
    if [ -n "$out" ]; then
      echo "ERROR: fuzzy entries in $po:" >&2
      echo "$out" >&2
      failed=1
    fi
  done
  if [ "$failed" -ne 0 ]; then
    echo "Translations incomplete: run scripts/i18n.sh make and translate the entries above." >&2
    exit 1
  fi
  do_compile
}

case "${1:-}" in
  make) do_make ;;
  compile) do_compile ;;
  check) do_check ;;
  *) echo "usage: $0 {make|compile|check}" >&2; exit 2 ;;
esac
