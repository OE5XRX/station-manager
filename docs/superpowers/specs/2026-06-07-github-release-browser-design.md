# GitHub release browser for image imports

Date: 2026-06-07

## Problem

The image-releases page (`/images/`) hosts a manual import form: the admin
types a release tag (`v1-alpha`, `2026.04.24-18`, …) and picks a machine
(`qemux86-64` / `raspberrypi4-64`) from a dropdown, one job per click.
Three rough edges:

1. **Tag-typing is friction and error-prone.** The tag has to match the
   GitHub release name exactly or the worker 404s on the asset URL. Admins
   need a separate tab open on `github.com/OE5XRX/linux-image/releases`
   just to copy the string.
2. **Two machines per release means two trips through the form** for the
   normal case (qemu + rpi). No visibility on what's already imported and
   what's still missing.
3. **No discovery.** A new release lands on GitHub and nothing on the
   station-manager surfaces it until somebody happens to look.

The fix: replace the manual form with a live list of GitHub release tags,
filtered to what's not yet in the DB, one "Queue" button per missing
`(tag, machine)` pair.

## Decisions

| Decision | Choice | Reasoning |
| --- | --- | --- |
| List granularity | One row per `(tag, machine)` pair | Matches the DB-unique key. A tag with one machine imported and one missing shows exactly one row. |
| List size cap | 10 newest tags by default; "Show all" toggle widens to 30 | Daily Yocto builds (see `gh release list`) would otherwise grow the list unboundedly. User explicitly rejected a persistent dismiss/hide mechanism. |
| Fetch strategy | Live fetch on every page-load, no cache | Admin-page traffic is low; GitHub's 60 req/h unauthenticated limit is not a realistic constraint. Simpler than a cache layer. |
| Auth to GitHub | Unauthenticated public API | Repo is public. No new secret to manage. |
| `mark_as_latest` default | Smart: `true` only if the release tag equals GitHub's "latest" at *page-load time*. Carried as a hidden form field through Queue submit. | Prevents accidental downgrade when re-importing an older tag. The current form's blanket `default=True` is unsafe on re-imports. Trusting the client value (instead of re-fetching `/releases/latest` on every Queue click) saves 1 API call per click — relevant under the 60 req/h unauthenticated limit. The narrow race (admin keeps the page open, a newer release appears, then clicks Queue on the older tag) results in a recoverable wrong-latest, fixable via the existing "Mark latest" button. Acceptable for an admin-only surface. |
| Asset validation | Show all tags; disable Queue button when the machine's expected assets are missing from the release | Visible signal that "this tag exists but isn't built for this machine yet", instead of a silent 404 in the worker after Queue. |
| Manual form | Removed completely | User explicitly chose "replace". If GitHub is unreachable, no manual tag could be queued anyway — the worker would 404 on the asset URL too. |
| UI pattern | HTMX inline-replace per row | Project already uses HTMX (Bootstrap 5 + HTMX per `CLAUDE.md`). Two GitHub API calls per page-view (list + `/releases/latest`), none per Queue click. |
| Persistence | None — no new model, no migration | All GitHub data is in-memory per request. State lives in `ImageImportJob` / `ImageRelease` as today. |

## Architecture

One new pure module, two new HTMX views, four new template partials, one
existing template restructured. No DB migration.

```
apps/images/
    github_releases.py            # NEW: GitHub API client (releases + latest)
    views.py                      # +GitHubReleasesPartialView, +QuickQueueView
                                  #   −ImageImportView (deleted)
    urls.py                       # +gh_partial, +gh_queue routes
                                  #   −import/ route (deleted)
    forms.py                      # −ImageImportForm (deleted)
    templates/images/
        image_list.html           # import-form panel replaced by gh-panel
        _github_panel.html        # NEW: container, hx-get on load
        _github_releases_table.html  # NEW: full table partial
        _github_release_row.html  # NEW: single row partial (post-queue replacement)
        _github_error.html        # NEW: error banner with retry button

tests/
    test_images_github_browser.py # NEW: client + view tests
    test_images.py                # remove ImageImportView/Form tests
```

The existing `apps/images/github.py` (asset-download client used by the
worker) is unchanged. Naming-wise `github_releases.py` is intentional:
`github.py` deals with *one asset of one known release*, the new module
deals with *listing releases*. Different concerns, different module.

## Data flow

```
GET /images/
  └─ image_list.html renders {% include "images/_github_panel.html" %}
  └─ container div has hx-get="/images/github-releases/?show=newest"
                       hx-trigger="load" hx-swap="innerHTML"
  └─ shows a small spinner while loading

GET /images/github-releases/?show=newest|all       (HTMX partial)
  └─ github_releases.fetch_releases(repo, limit=30)   ← 2 API calls
       1. GET https://api.github.com/repos/{repo}/releases?per_page=30
       2. GET https://api.github.com/repos/{repo}/releases/latest
  └─ for each release, for each machine in (qemux86-64, raspberrypi4-64):
        decide row state:
          - imported   → row hidden (skip entirely)
          - queued     → row visible, button disabled, "QUEUED" badge
          - no_asset   → row visible, button disabled, "no asset" badge
          - ready      → row visible, button enabled
  └─ if show=newest: keep first 10 tags that have ≥1 visible row
       if show=all:    keep all 30
  └─ renders _github_releases_table.html

POST /images/github-releases/queue/                 (HTMX POST)
  body: tag=…&machine=…&is_latest=0|1
  └─ re-validate against current DB state (race-safe; see §Error handling)
  └─ ImageImportJob.objects.create(
          tag=tag, machine=machine,
          mark_as_latest = (is_latest == "1"),     # client-supplied, see Decisions
          requested_by   = request.user,
     )
  └─ renders _github_release_row.html with state=queued
       (HTMX replaces the row in place via hx-swap="outerHTML")
```

## `apps/images/github_releases.py`

Pure module. No Django imports. Easy to unit-test with mocked
`urllib.request.urlopen`.

```python
from __future__ import annotations
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GITHUB_API = "https://api.github.com"
_USER_AGENT = "oe5xrx-station-manager"
_ACCEPT = "application/vnd.github+json"
_TIMEOUT = 10  # seconds


class GitHubAPIError(Exception):
    """Wraps HTTP / network / decode failures from GitHub's REST API."""


@dataclass(frozen=True)
class GitHubRelease:
    tag: str
    html_url: str
    is_latest: bool
    asset_names: frozenset[str]

    def has_assets_for(self, machine: str) -> bool:
        prefix = f"oe5xrx-{machine}-{self.tag}.wic.bz2"
        return all(
            name in self.asset_names
            for name in (prefix, f"{prefix}.bundle", f"{prefix}.sha256")
        )


def fetch_releases(repo: str, limit: int = 30) -> list[GitHubRelease]:
    """Return releases newest-first, with is_latest annotated.

    Two API calls: list (paginated to `limit`) + /releases/latest. If
    /releases/latest 404s (repo has only prereleases), is_latest is False
    for all entries.

    Raises GitHubAPIError on any HTTP, timeout, or decode failure.
    """
    raw = _get_json(f"{GITHUB_API}/repos/{repo}/releases?per_page={limit}")
    try:
        latest = _get_json(f"{GITHUB_API}/repos/{repo}/releases/latest")
        latest_tag = latest.get("tag_name")
    except GitHubAPIError:
        latest_tag = None
    return [
        GitHubRelease(
            tag=r["tag_name"],
            html_url=r.get("html_url", ""),
            is_latest=(r["tag_name"] == latest_tag),
            asset_names=frozenset(a["name"] for a in r.get("assets", [])),
        )
        for r in raw
    ]


def _get_json(url: str) -> object:
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GitHubAPIError(str(exc)) from exc
```

`GitHubAPIError` is caught in the view, never propagates to the user.

## `apps/images/views.py`

```python
from enum import Enum

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import render
from django.utils.translation import gettext as _
from django.views import View

from . import github_releases
from .models import ImageImportJob, ImageRelease

MACHINES = [ImageRelease.Machine.QEMU, ImageRelease.Machine.RPI]
NEWEST_LIMIT = 10
ALL_LIMIT = 30


class RowState(str, Enum):
    READY = "ready"
    QUEUED = "queued"
    NO_ASSET = "no_asset"
    # IMPORTED rows are filtered out before rendering.


class GitHubReleasesPartialView(AdminRequiredMixin, View):
    def get(self, request):
        show = request.GET.get("show", "newest")
        # We always fetch up to ALL_LIMIT from GitHub because the post-filter
        # (skip already-imported tags) can drop arbitrary rows; pulling only
        # NEWEST_LIMIT could leave us with an empty page when the top 10
        # tags are already imported. The newest/all switch only affects how
        # many *visible* tags we render.
        try:
            releases = github_releases.fetch_releases(
                getattr(settings, "LINUX_IMAGE_REPO", "OE5XRX/linux-image"),
                limit=ALL_LIMIT,
            )
        except github_releases.GitHubAPIError as exc:
            return render(request, "images/_github_error.html", {"error": str(exc)})

        imported = set(
            ImageRelease.objects.values_list("tag", "machine")
        )
        in_flight = set(
            ImageImportJob.objects.filter(
                status__in=[ImageImportJob.Status.PENDING, ImageImportJob.Status.RUNNING],
            ).values_list("tag", "machine")
        )

        rows_by_tag = []   # list[(GitHubRelease, list[(machine, state)])]
        for rel in releases:
            machine_rows = []
            for m in MACHINES:
                key = (rel.tag, m)
                if key in imported:
                    continue
                if key in in_flight:
                    state = RowState.QUEUED
                elif not rel.has_assets_for(m):
                    state = RowState.NO_ASSET
                else:
                    state = RowState.READY
                machine_rows.append((m, state))
            if machine_rows:
                rows_by_tag.append((rel, machine_rows))

        if show == "newest":
            rows_by_tag = rows_by_tag[:NEWEST_LIMIT]

        return render(
            request,
            "images/_github_releases_table.html",
            {"rows_by_tag": rows_by_tag, "show": show},
        )


class QuickQueueView(AdminRequiredMixin, View):
    def post(self, request):
        tag = request.POST.get("tag", "").strip()
        machine = request.POST.get("machine", "").strip()
        is_latest = request.POST.get("is_latest", "0") == "1"
        if not tag or machine not in {m.value for m in MACHINES}:
            return HttpResponseBadRequest("invalid tag/machine")

        # Race-safe re-check against current DB state.
        if ImageRelease.objects.filter(tag=tag, machine=machine).exists():
            return _render_row(request, tag, machine, is_latest=is_latest, state="imported")
        existing = ImageImportJob.objects.filter(
            tag=tag, machine=machine,
            status__in=[ImageImportJob.Status.PENDING, ImageImportJob.Status.RUNNING],
        ).first()
        if existing:
            return _render_row(request, tag, machine, is_latest=is_latest, state="queued")

        ImageImportJob.objects.create(
            tag=tag, machine=machine,
            mark_as_latest=is_latest,
            requested_by=request.user,
        )
        return _render_row(request, tag, machine, is_latest=is_latest, state="queued")


def _render_row(request, tag, machine, is_latest, state, html_url=""):
    return render(
        request,
        "images/_github_release_row.html",
        {"tag": tag, "machine": machine, "is_latest": is_latest,
         "state": state, "html_url": html_url},
    )
```

`ImageImportView` is deleted entirely. `ImageImportForm` in `forms.py` is
deleted. `image_list.html` no longer references `import_form`.

## Templates

**`_github_panel.html`** — wraps the table in a card matching the
existing `panel` aesthetic (see `image_list.html` for the visual
language). Container has:

```html
<div id="gh-releases"
     hx-get="{% url 'images:gh_partial' %}?show=newest"
     hx-trigger="load"
     hx-swap="innerHTML">
  <div class="empty">{% trans "Loading releases from GitHub…" %}</div>
</div>
```

A small header above with a "Refresh" button that re-triggers
`hx-get` on the same URL, and a "Show all" / "Show newest 10" toggle
that swaps `?show=…`.

**`_github_releases_table.html`** — table with thead `Tag | Machine |
Asset | Action`. Rows are grouped visually by tag (first row of each
group repeats the tag). Each row renders:

```html
{% include "images/_github_release_row.html" with
   tag=rel.tag machine=m state=state
   is_latest=rel.is_latest html_url=rel.html_url %}
```

**`_github_release_row.html`** — single `<tr>` keyed by both an HTML
`id` and a `data-gh-row` attribute. The Queue button targets the row via
**attribute selector** (`[data-gh-row='…']`), not an ID selector — real
Yocto tags contain dots (`2026.04.24-18`), and `#gh-row-2026.04.24-18-…`
gets parsed as `#gh-row-2026` + `.04` + `.24-18-…` (class selectors) by
CSS, breaking the HTMX swap.

```html
<tr id="gh-row-{{ tag }}-{{ machine }}" data-gh-row="{{ tag }}-{{ machine }}">
  <td>
    {% if html_url %}
      <a href="{{ html_url }}" target="_blank" rel="noopener">{{ tag }}</a>
    {% else %}
      {{ tag }}
    {% endif %}
    {% if is_latest %}<span class="pill pill-accent">LATEST</span>{% endif %}
  </td>
  <td>{{ machine }}</td>
  <td>
    {% if state == "no_asset" %}
      <span class="pill pill-muted">no asset</span>
    {% else %}
      <span class="t-muted">ok</span>
    {% endif %}
  </td>
  <td>
    {% if state == "ready" %}
      <button class="btn btn-primary btn-sm"
              hx-post="{% url 'images:gh_queue' %}"
              hx-vals='{"tag": "{{ tag|escapejs }}", "machine": "{{ machine|escapejs }}", "is_latest": "{% if is_latest %}1{% else %}0{% endif %}"}'
              hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'
              hx-target="[data-gh-row='{{ tag }}-{{ machine }}']"
              hx-swap="outerHTML">{% trans "Queue" %}</button>
    {% elif state == "queued" %}
      <span class="pill pill-pending">{% trans "QUEUED" %}</span>
    {% elif state == "imported" %}
      <span class="pill pill-online">{% trans "IMPORTED" %}</span>
    {% elif state == "no_asset" %}
      <button class="btn btn-sm" disabled>{% trans "Queue" %}</button>
    {% endif %}
  </td>
</tr>
```

The post-Queue render path goes through `_render_row` in the view, which
passes `html_url=""` (the URL was only needed before the click; after
Queue the row is a frozen result, no link needed). The template's
`{% if html_url %}` guard handles both cases.

**`_github_error.html`** — banner with the error message and a "Try
again" button (`hx-get` on `images:gh_partial`).

## Error handling

| Failure | Behaviour |
| --- | --- |
| GitHub API 4xx / 5xx / timeout on the list fetch | Partial renders `_github_error.html`. Banner says "GitHub temporarily unreachable: {reason}". Try-again button re-issues `hx-get`. No table, no spinner-loop. |
| GitHub API failure on `/releases/latest` only | `is_latest` is `false` for all rows. List still renders. (Handled inside `fetch_releases`.) |
| Quick-Queue called for a tag that was GitHub-latest at page-load but isn't anymore | Tag is queued with `mark_as_latest=true` from the stale hidden field. Recoverable via existing "Mark latest" button on the upper table. See the `mark_as_latest` decision row for the trade-off. |
| Race: `ImageRelease(tag, machine)` already created between page-load and queue click | Row replaced with state=`imported` (visible "IMPORTED" pill, no job created). |
| Race: `ImageImportJob(tag, machine)` already pending/running between page-load and queue click | Row replaced with state=`queued`, no second job. |
| Invalid `tag` / `machine` in POST | `HttpResponseBadRequest` (HTMX shows nothing; only reachable by tampering, real users can't hit it). |

## Testing

New file `tests/test_images_github_browser.py`:

**`fetch_releases`** (unit, mocked `urlopen`):
- Happy: 3 releases + /latest match → correct dataclasses, `is_latest=True` only on the matching tag.
- /latest 404s → all rows `is_latest=False`, list still renders.
- /releases URLError → raises `GitHubAPIError`.
- Timeout → raises `GitHubAPIError`.
- Malformed JSON → raises `GitHubAPIError`.

**`GitHubRelease.has_assets_for`** (unit):
- All 3 expected names present → True.
- Missing `.bundle` → False.
- Missing `.sha256` → False.

**`GitHubReleasesPartialView`** (Django test client, mocked `fetch_releases`):
- Empty release list → empty-state table (no rows, header still rendered).
- 12 releases, none imported, all assets present → 10 rows visible (default newest).
- `?show=all` → all 12 visible.
- Tag with both machines already imported → tag omitted entirely.
- Tag with qemu imported, rpi missing → one row (rpi), no qemu row.
- `(tag, rpi)` has pending job → row visible, state=queued, button disabled.
- Release missing rpi assets → rpi row visible, state=no_asset, button disabled.
- `fetch_releases` raises `GitHubAPIError` → error partial rendered.
- Non-admin user → 403 (covered by AdminRequiredMixin, smoke-test once).

**`QuickQueueView`** (Django test client; no GitHub mock needed — POST is self-contained):
- Happy path, `is_latest=1` in POST → job created with `mark_as_latest=True`, response is row partial with QUEUED state.
- Happy path, `is_latest=0` in POST → `mark_as_latest=False`.
- `is_latest` field omitted → treated as `0` (defaults to False).
- Tag already in `ImageRelease` → no job created, response is row partial with IMPORTED state.
- Pending job already exists → no second job created, response is row partial with QUEUED state.
- Invalid machine string → 400.
- Missing tag → 400.

**Removed from `tests/test_images.py`:**
- Any test exercising `ImageImportView`, `ImageImportForm`, or the `import/` URL. The
  worker-level test `process_pending_image_imports` stays untouched (it consumes
  `ImageImportJob` rows regardless of who created them).

## Out of scope

- **Caching.** Live-fetch per page view. If 60 req/h ever bites we can add a
  short server-side cache; not yet.
- **GITHUB_TOKEN support.** Public repo, no need. Adding the auth header
  later is one-line.
- **Auto-refresh / polling.** Manual Refresh button is enough.
- **Bulk-queue / multi-select.** Per-row Queue button is the chosen UX.
- **Dismiss/hide tags.** Show-all toggle covers the edge case.
- **Reordering / search.** 10 newest by default already keeps the surface
  small.
- **Visual changes to the upper "Imported images" table or the "Recent
  import jobs" timeline.** Those panels stay as-is.
