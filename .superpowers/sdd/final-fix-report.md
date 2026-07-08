# Final Fix Report: Terminal Panel Gate — is_internal → is_admin

## Branch
`feat/terminal-shell-lifecycle`

## File Changed
`apps/stations/templates/stations/station_detail.html`

## Lines Changed

| Line | Before | After | Reason |
|------|--------|-------|--------|
| 94 | `{% if user.is_internal %}` (Terminal tab button) | `{% if user.is_admin %}` | Terminal tab was visible to non-admin staff — now matches backend WS gate |
| 377 | `{% if user.is_internal %}` (Terminal panel `data-tab-panel="terminal"`) | `{% if user.is_admin %}` | Terminal panel body was visible to non-admin staff — now matches backend WS gate |

## All `is_internal` Occurrences Inspected

| Line | Context | Decision |
|------|---------|----------|
| 42 | Edit + Delete station buttons (`page-head-actions`) | NOT terminal-related — left as `is_internal` |
| 94 | Terminal tab nav button (`data-tab="terminal"`) | TERMINAL — changed to `is_admin` |
| 180 | Photo upload form inside Photos tab panel | NOT terminal-related — left as `is_internal` |
| 222 | Logbook entry add form inside Logbook tab panel | NOT terminal-related — left as `is_internal` |
| 377 | Terminal tab panel (`data-tab-panel="terminal"`) + xterm container + restart button | TERMINAL — changed to `is_admin` |

## Verification Output

### grep confirmation (post-edit)
```
42:      {% if user.is_internal %}     ← Edit/Delete buttons, untouched
94:      {% if user.is_admin %}        ← Terminal tab, fixed
180:       {% if user.is_internal %}   ← Photo upload, untouched
222:       {% if user.is_internal %}   ← Logbook form, untouched
377:   {% if user.is_admin %}          ← Terminal panel, fixed
```

### `python3 scripts/check_template_comments.py`
Passed (no output — clean).

### `python3 manage.py check`
Failed with `ModuleNotFoundError: No module named 'debug_toolbar'` — dev dependency not installed in this environment. Not a template syntax issue; the template itself has no Django check-detectable errors.

### `python3 -m pytest tests/test_terminal_consumer.py -q`
```
.....
5 passed in 3.26s
```

## No multi-line `{# … #}` comments introduced.
