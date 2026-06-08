# GitHub Release Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual tag-typing image-import form with a live list of GitHub releases, one Queue button per missing `(tag, machine)` pair, with smart `mark_as_latest` and asset-presence checks.

**Architecture:** New pure module `apps/images/github_releases.py` for the GitHub REST client (releases + latest, two API calls per page-view, no caching, unauthenticated). Two new HTMX views: `GitHubReleasesPartialView` (GET, renders the full table) and `QuickQueueView` (POST, creates an `ImageImportJob` and returns a single-row partial). Four new template partials. Existing `ImageImportView` / `ImageImportForm` / `import/` route are deleted.

**Tech Stack:** Django 6.0 CBV, HTMX 2.x (already in base.html), pytest + pytest-django, `urllib.request` for the GitHub API (no new deps), `apps.accounts.views.AdminRequiredMixin` for auth.

**Reference spec:** `docs/superpowers/specs/2026-06-07-github-release-browser-design.md`

**Out of scope (per spec §Out of scope):** caching, GITHUB_TOKEN auth, polling, bulk-queue, persistent dismiss, visual changes to upper "Imported images" table / "Recent import jobs" timeline.

---

## In-Tree State (verified 2026-06-07)

| Item | Reality |
|---|---|
| Test runner | `pytest` with `DJANGO_SETTINGS_MODULE=config.settings.test` |
| Admin fixture | `admin_user` in `tests/conftest.py` — `MembershipLevel.ADMIN` |
| Operator fixture | `operator_user` — `MembershipLevel.STAFF` |
| Admin gate | `from apps.accounts.views import AdminRequiredMixin` (already imported in `apps/images/views.py`) |
| Existing GitHub client | `apps/images/github.py` — asset download for the worker. Unchanged by this plan. |
| Worker | `apps/provisioning/management/commands/run_background_jobs.py::_run_import_job` — claims `ImageImportJob.Status.PENDING`. Unchanged. |
| Repo env | `LINUX_IMAGE_REPO` (default `OE5XRX/linux-image`), read via `getattr(settings, ...)` |
| `ImageRelease.Machine` | `TextChoices`: `QEMU="qemux86-64"`, `RPI="raspberrypi4-64"` |
| URL namespace | `images:` — current routes: `list`, `import`, `mark_latest`, `delete` |
| HTMX | already loaded in `templates/base.html`; CSP nonce via `{{ csp_nonce }}` if needed |
| Working dir | `/home/pbuchegger/OE5XRX/station-manager/.worktrees/github-release-browser/` (branch `feature/github-release-browser`) |

---

## File Structure

### New

| Path | Responsibility |
|---|---|
| `apps/images/github_releases.py` | Pure module: `GitHubRelease` dataclass, `GitHubAPIError`, `fetch_releases(repo, limit)` |
| `apps/images/templates/images/_github_panel.html` | Outer card with header (title, Refresh, Show-all toggle) and HTMX-load container |
| `apps/images/templates/images/_github_releases_table.html` | Full `<table>` rendered into the container on initial GET / Refresh |
| `apps/images/templates/images/_github_release_row.html` | Single `<tr>` — used both inside the table and as the POST response from Quick-Queue |
| `apps/images/templates/images/_github_error.html` | Error banner with Try-Again button |
| `tests/test_images_github_browser.py` | All tests for `github_releases.py` + the two new views |

### Modified

| Path | Reason |
|---|---|
| `apps/images/views.py` | Add `GitHubReleasesPartialView`, `QuickQueueView`; remove `ImageImportView` |
| `apps/images/urls.py` | Add `gh_partial`, `gh_queue` routes; remove `import/` route |
| `apps/images/forms.py` | Remove `ImageImportForm` (file becomes empty → delete the file) |
| `apps/images/templates/images/image_list.html` | Replace the "Import from GitHub" form panel with `{% include "images/_github_panel.html" %}`; drop `import_form` context |
| `tests/test_images.py` | Remove `TestImportView` (3 tests) — they exercise the deleted view/form. |

---

# Task 1: `GitHubRelease` dataclass + asset check

**Files:**
- Create: `apps/images/github_releases.py`
- Create: `tests/test_images_github_browser.py`

The dataclass and its `has_assets_for()` method are pure logic — TDD-friendly. No network, no Django.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_images_github_browser.py`:

```python
from apps.images.github_releases import GitHubRelease


class TestGitHubReleaseAssets:
    def _release(self, names):
        return GitHubRelease(
            tag="v1-alpha",
            html_url="https://example.invalid/v1-alpha",
            is_latest=False,
            asset_names=frozenset(names),
        )

    def test_all_three_assets_present_returns_true(self):
        rel = self._release([
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.bundle",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.sha256",
        ])
        assert rel.has_assets_for("qemux86-64") is True

    def test_missing_bundle_returns_false(self):
        rel = self._release([
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.sha256",
        ])
        assert rel.has_assets_for("qemux86-64") is False

    def test_missing_sha256_returns_false(self):
        rel = self._release([
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.bundle",
        ])
        assert rel.has_assets_for("qemux86-64") is False

    def test_missing_wic_returns_false(self):
        rel = self._release([
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.bundle",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.sha256",
        ])
        assert rel.has_assets_for("qemux86-64") is False

    def test_other_machine_assets_dont_satisfy(self):
        rel = self._release([
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.bundle",
            "oe5xrx-qemux86-64-v1-alpha.wic.bz2.sha256",
        ])
        assert rel.has_assets_for("raspberrypi4-64") is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'apps.images.github_releases'`.

- [ ] **Step 3: Implement minimal module**

Create `apps/images/github_releases.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add apps/images/github_releases.py tests/test_images_github_browser.py
git commit -m "feat(images): GitHubRelease dataclass with asset check"
```

---

# Task 2: `fetch_releases` happy path

**Files:**
- Modify: `apps/images/github_releases.py`
- Modify: `tests/test_images_github_browser.py`

`fetch_releases` makes two API calls: `GET /repos/{repo}/releases?per_page={limit}` and `GET /repos/{repo}/releases/latest`. It annotates `is_latest` on the matching tag.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_images_github_browser.py`:

```python
import io
import json
from unittest.mock import patch

import pytest

from apps.images.github_releases import (
    GitHubAPIError,
    GitHubRelease,
    fetch_releases,
)


def _gh_response(payload):
    """Return a fake urlopen context manager yielding JSON bytes."""

    class _CM:
        def __enter__(self):
            return io.BytesIO(json.dumps(payload).encode("utf-8"))

        def __exit__(self, *a):
            return False

    return _CM()


def _make_fake_urlopen(responses_by_url):
    def fake(req, timeout):
        url = req.full_url if hasattr(req, "full_url") else req
        if url not in responses_by_url:
            raise AssertionError(f"unexpected url {url}")
        return _gh_response(responses_by_url[url])

    return fake


class TestFetchReleasesHappy:
    def test_returns_releases_newest_first_with_is_latest(self):
        list_url = "https://api.github.com/repos/OE5XRX/linux-image/releases?per_page=30"
        latest_url = "https://api.github.com/repos/OE5XRX/linux-image/releases/latest"
        responses = {
            list_url: [
                {
                    "tag_name": "v2",
                    "html_url": "https://github.com/x/v2",
                    "assets": [{"name": "oe5xrx-qemux86-64-v2.wic.bz2"}],
                },
                {
                    "tag_name": "v1",
                    "html_url": "https://github.com/x/v1",
                    "assets": [],
                },
            ],
            latest_url: {"tag_name": "v2"},
        }

        with patch(
            "apps.images.github_releases.urllib.request.urlopen",
            side_effect=_make_fake_urlopen(responses),
        ):
            result = fetch_releases("OE5XRX/linux-image", limit=30)

        assert len(result) == 2
        assert result[0].tag == "v2"
        assert result[0].is_latest is True
        assert result[0].html_url == "https://github.com/x/v2"
        assert result[0].asset_names == frozenset(["oe5xrx-qemux86-64-v2.wic.bz2"])
        assert result[1].tag == "v1"
        assert result[1].is_latest is False
        assert result[1].asset_names == frozenset()

    def test_empty_release_list_returns_empty(self):
        list_url = "https://api.github.com/repos/OE5XRX/linux-image/releases?per_page=30"
        latest_url = "https://api.github.com/repos/OE5XRX/linux-image/releases/latest"

        # /releases/latest is allowed to 404 when there are no releases at all;
        # we mock it as 200 with a non-matching tag so the latest-tag branch runs cleanly.
        responses = {list_url: [], latest_url: {"tag_name": "nope"}}

        with patch(
            "apps.images.github_releases.urllib.request.urlopen",
            side_effect=_make_fake_urlopen(responses),
        ):
            assert fetch_releases("OE5XRX/linux-image", limit=30) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_images_github_browser.py::TestFetchReleasesHappy -v`
Expected: 2 errors — `ImportError: cannot import name 'fetch_releases'`.

- [ ] **Step 3: Implement `fetch_releases`**

Modify `apps/images/github_releases.py` — extend to:

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

    Two API calls: list (paginated to `limit`) + /releases/latest.
    Raises GitHubAPIError on any HTTP, timeout, or decode failure on the
    list call. /releases/latest failures are tolerated (is_latest=False
    for all rows).
    """
    raw = _get_json(f"{GITHUB_API}/repos/{repo}/releases?per_page={limit}")
    try:
        latest = _get_json(f"{GITHUB_API}/repos/{repo}/releases/latest")
        latest_tag = latest.get("tag_name") if isinstance(latest, dict) else None
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


def _get_json(url: str):
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": _ACCEPT}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise GitHubAPIError(str(exc)) from exc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 7 passed (5 from Task 1 + 2 from Task 2).

- [ ] **Step 5: Commit**

```bash
git add apps/images/github_releases.py tests/test_images_github_browser.py
git commit -m "feat(images): fetch_releases happy path"
```

---

# Task 3: `fetch_releases` error paths

**Files:**
- Modify: `tests/test_images_github_browser.py`
- (No code changes needed — error wrapping already in place from Task 2.)

We added the error-wrapping in Task 2 for simplicity. This task locks the behaviour down with tests so a future refactor can't silently break it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_images_github_browser.py`:

```python
import urllib.error

class TestFetchReleasesErrors:
    def test_list_url_error_raises_github_api_error(self):
        def fake(req, timeout):
            raise urllib.error.URLError("no route to host")

        with patch(
            "apps.images.github_releases.urllib.request.urlopen", side_effect=fake
        ):
            with pytest.raises(GitHubAPIError) as exc:
                fetch_releases("OE5XRX/linux-image")
            assert "no route to host" in str(exc.value)

    def test_list_timeout_raises_github_api_error(self):
        def fake(req, timeout):
            raise TimeoutError("read timed out")

        with patch(
            "apps.images.github_releases.urllib.request.urlopen", side_effect=fake
        ):
            with pytest.raises(GitHubAPIError):
                fetch_releases("OE5XRX/linux-image")

    def test_list_malformed_json_raises_github_api_error(self):
        class _BadJSON:
            def __enter__(self):
                return io.BytesIO(b"<<not json>>")

            def __exit__(self, *a):
                return False

        with patch(
            "apps.images.github_releases.urllib.request.urlopen",
            return_value=_BadJSON(),
        ):
            with pytest.raises(GitHubAPIError):
                fetch_releases("OE5XRX/linux-image")

    def test_latest_url_failure_keeps_list_renderable(self):
        """If /releases/latest fails (e.g. repo has only prereleases),
        the list still renders with is_latest=False on every row."""
        list_url = (
            "https://api.github.com/repos/OE5XRX/linux-image/releases?per_page=30"
        )
        latest_url = (
            "https://api.github.com/repos/OE5XRX/linux-image/releases/latest"
        )

        def fake(req, timeout):
            url = req.full_url
            if url == list_url:
                return _gh_response(
                    [{"tag_name": "v1", "html_url": "", "assets": []}]
                )
            if url == latest_url:
                raise urllib.error.HTTPError(
                    url, 404, "Not Found", hdrs=None, fp=None
                )
            raise AssertionError(f"unexpected url {url}")

        with patch(
            "apps.images.github_releases.urllib.request.urlopen", side_effect=fake
        ):
            result = fetch_releases("OE5XRX/linux-image")

        assert len(result) == 1
        assert result[0].is_latest is False
```

- [ ] **Step 2: Run tests to verify they pass (already implemented)**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 11 passed total.

Note: `urllib.error.HTTPError` is a subclass of `URLError`, so it's caught by the `except (URLError, ...)` clause already in `_get_json`.

- [ ] **Step 3: Commit**

```bash
git add tests/test_images_github_browser.py
git commit -m "test(images): lock down fetch_releases error wrapping"
```

---

# Task 4: URL routing + empty view stubs

**Files:**
- Modify: `apps/images/views.py`
- Modify: `apps/images/urls.py`
- Modify: `tests/test_images_github_browser.py`

Add minimal stub views so URLs reverse and admin gating tests can run before we layer in the real logic.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_images_github_browser.py`:

```python
from django.urls import reverse


@pytest.mark.django_db
class TestRoutingAndAuth:
    def test_partial_url_reverses(self):
        assert reverse("images:gh_partial") == "/images/github-releases/"

    def test_queue_url_reverses(self):
        assert reverse("images:gh_queue") == "/images/github-releases/queue/"

    def test_partial_requires_admin(self, client, operator_user):
        client.force_login(operator_user)
        response = client.get(reverse("images:gh_partial"))
        assert response.status_code == 403

    def test_queue_requires_admin(self, client, operator_user):
        client.force_login(operator_user)
        response = client.post(reverse("images:gh_queue"))
        assert response.status_code == 403

    def test_partial_anonymous_redirects(self, client):
        response = client.get(reverse("images:gh_partial"))
        # AdminRequiredMixin -> LoginRequiredMixin -> 302
        assert response.status_code == 302
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_images_github_browser.py::TestRoutingAndAuth -v`
Expected: `django.urls.exceptions.NoReverseMatch: Reverse for 'gh_partial' not found.`

- [ ] **Step 3: Add view stubs**

Modify `apps/images/views.py` — append (do NOT touch existing imports yet, just add):

```python
from django.http import HttpResponse, HttpResponseBadRequest

# ... existing imports / classes unchanged ...


class GitHubReleasesPartialView(AdminRequiredMixin, View):
    def get(self, request):
        return HttpResponse("")  # filled in next task


class QuickQueueView(AdminRequiredMixin, View):
    def post(self, request):
        return HttpResponse("")  # filled in later task
```

(`View` is already imported in this file; check the existing `ImageMarkLatestView` for reference.)

- [ ] **Step 4: Add URL routes**

Modify `apps/images/urls.py` to:

```python
from django.urls import path

from . import views

app_name = "images"

urlpatterns = [
    path("", views.ImageListView.as_view(), name="list"),
    path("import/", views.ImageImportView.as_view(), name="import"),
    path(
        "github-releases/",
        views.GitHubReleasesPartialView.as_view(),
        name="gh_partial",
    ),
    path(
        "github-releases/queue/",
        views.QuickQueueView.as_view(),
        name="gh_queue",
    ),
    path("<int:pk>/mark-latest/", views.ImageMarkLatestView.as_view(), name="mark_latest"),
    path("<int:pk>/delete/", views.ImageDeleteView.as_view(), name="delete"),
]
```

(`import/` route still here — removed in Task 10. Keeping it now means the existing `TestImportView` tests keep passing as we work.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 16 passed (11 + 5).

- [ ] **Step 6: Commit**

```bash
git add apps/images/views.py apps/images/urls.py tests/test_images_github_browser.py
git commit -m "feat(images): URL routes + view stubs for GitHub browser"
```

---

# Task 5: Template scaffolding

**Files:**
- Create: `apps/images/templates/images/_github_panel.html`
- Create: `apps/images/templates/images/_github_releases_table.html`
- Create: `apps/images/templates/images/_github_release_row.html`
- Create: `apps/images/templates/images/_github_error.html`

Empty-but-renderable templates so the next task can `render(...)` them without `TemplateDoesNotExist`.

- [ ] **Step 1: Create `_github_panel.html`**

```html
{% load i18n %}
<section class="panel mb-24" id="import-form">
  <div class="panel-head">
    <div class="panel-title">
      <span class="dot" style="background:var(--accent);"></span>
      {% trans "Import from GitHub" %}
    </div>
    <span class="t-label">{% trans "Live list of release tags" %}</span>
  </div>
  <div class="panel-body flush">
    <div class="row-gap-8" style="padding:12px 16px;border-bottom:1px solid var(--line);">
      <a class="btn btn-sm"
         hx-get="{% url 'images:gh_partial' %}"
         hx-target="#gh-releases"
         hx-swap="innerHTML">
        {% trans "Refresh" %}
      </a>
      <a class="btn btn-sm"
         hx-get="{% url 'images:gh_partial' %}?show=all"
         hx-target="#gh-releases"
         hx-swap="innerHTML">
        {% trans "Show all" %}
      </a>
      <a class="btn btn-sm"
         hx-get="{% url 'images:gh_partial' %}"
         hx-target="#gh-releases"
         hx-swap="innerHTML">
        {% trans "Show newest 10" %}
      </a>
    </div>
    <div id="gh-releases"
         hx-get="{% url 'images:gh_partial' %}"
         hx-trigger="load"
         hx-swap="innerHTML">
      <div class="empty" style="padding:28px 12px;">
        <div class="empty-title">{% trans "Loading releases from GitHub…" %}</div>
      </div>
    </div>
  </div>
</section>
```

- [ ] **Step 2: Create `_github_releases_table.html`**

```html
{% load i18n %}
{% if rows_by_tag %}
<div class="table-wrap" data-mobile-cards style="border:none;border-radius:0;">
  <table class="t-table">
    <thead>
      <tr>
        <th>{% trans "Tag" %}</th>
        <th>{% trans "Machine" %}</th>
        <th>{% trans "Asset" %}</th>
        <th style="width:1%;">{% trans "Action" %}</th>
      </tr>
    </thead>
    <tbody>
      {% for rel, machine_rows in rows_by_tag %}
        {% for m, state in machine_rows %}
          {% include "images/_github_release_row.html" with tag=rel.tag machine=m state=state is_latest=rel.is_latest html_url=rel.html_url %}
        {% endfor %}
      {% endfor %}
    </tbody>
  </table>
</div>
{% else %}
<div class="empty" style="padding:28px 12px;">
  <div class="empty-title">{% trans "No new releases to import" %}</div>
  <div>{% trans "All current GitHub releases are already in the database." %}</div>
</div>
{% endif %}
```

- [ ] **Step 3: Create `_github_release_row.html`**

```html
{% load i18n %}
<tr id="gh-row-{{ tag }}-{{ machine }}">
  <td data-label="{% trans 'Tag' %}">
    {% if html_url %}
      <a href="{{ html_url }}" target="_blank" rel="noopener"
         style="font-family:var(--font-mono);font-weight:600;color:var(--ink-0);">{{ tag }}</a>
    {% else %}
      <span style="font-family:var(--font-mono);font-weight:600;color:var(--ink-0);">{{ tag }}</span>
    {% endif %}
    {% if is_latest %}
      <span class="pill pill-accent" style="margin-left:6px;">{% trans "LATEST" %}</span>
    {% endif %}
  </td>
  <td data-label="{% trans 'Machine' %}">
    {% if machine == "raspberrypi4-64" %}
      <span class="pill pill-violet">RPi 4 · 64</span>
    {% elif machine == "qemux86-64" %}
      <span class="pill pill-accent">QEMU x86-64</span>
    {% else %}
      <span class="pill pill-muted">{{ machine }}</span>
    {% endif %}
  </td>
  <td data-label="{% trans 'Asset' %}">
    {% if state == "no_asset" %}
      <span class="pill pill-muted" title="{% trans 'release does not contain an asset for this machine' %}">{% trans "no asset" %}</span>
    {% else %}
      <span class="t-mono-sm t-muted">{% trans "ok" %}</span>
    {% endif %}
  </td>
  <td class="actions" data-label="{% trans 'Action' %}">
    {% if state == "ready" %}
      <button class="btn btn-sm btn-primary"
              hx-post="{% url 'images:gh_queue' %}"
              hx-vals='{"tag": "{{ tag|escapejs }}", "machine": "{{ machine|escapejs }}", "is_latest": "{% if is_latest %}1{% else %}0{% endif %}"}'
              hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'
              hx-target="#gh-row-{{ tag }}-{{ machine }}"
              hx-swap="outerHTML">
        {% trans "Queue" %}
      </button>
    {% elif state == "queued" %}
      <span class="pill pill-pending"><span class="dot"></span>{% trans "QUEUED" %}</span>
    {% elif state == "imported" %}
      <span class="pill pill-online"><span class="dot"></span>{% trans "IMPORTED" %}</span>
    {% elif state == "no_asset" %}
      <button class="btn btn-sm" disabled>{% trans "Queue" %}</button>
    {% endif %}
  </td>
</tr>
```

- [ ] **Step 4: Create `_github_error.html`**

```html
{% load i18n %}
<div class="empty" style="padding:28px 12px;">
  <div class="empty-title">{% trans "GitHub temporarily unreachable" %}</div>
  <div class="t-mono-sm t-muted" style="margin:6px 0 12px;">{{ error }}</div>
  <button class="btn btn-sm"
          hx-get="{% url 'images:gh_partial' %}"
          hx-target="#gh-releases"
          hx-swap="innerHTML">
    {% trans "Try again" %}
  </button>
</div>
```

- [ ] **Step 5: Run existing tests (sanity)**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 16 passed — no regressions.

- [ ] **Step 6: Commit**

```bash
git add apps/images/templates/images/_github_panel.html \
        apps/images/templates/images/_github_releases_table.html \
        apps/images/templates/images/_github_release_row.html \
        apps/images/templates/images/_github_error.html
git commit -m "feat(images): GitHub browser template scaffolding"
```

---

# Task 6: `GitHubReleasesPartialView` — happy paths

**Files:**
- Modify: `apps/images/views.py`
- Modify: `tests/test_images_github_browser.py`

Real logic: build `rows_by_tag`, filter, slice, render.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_images_github_browser.py`:

```python
from apps.images.models import ImageImportJob, ImageRelease


def _mk_release(tag, is_latest=False, machines=("qemux86-64", "raspberrypi4-64")):
    names = set()
    for m in machines:
        prefix = f"oe5xrx-{m}-{tag}.wic.bz2"
        names.update([prefix, f"{prefix}.bundle", f"{prefix}.sha256"])
    return GitHubRelease(
        tag=tag,
        html_url=f"https://github.com/x/{tag}",
        is_latest=is_latest,
        asset_names=frozenset(names),
    )


@pytest.mark.django_db
class TestGitHubReleasesPartialViewHappy:
    def test_empty_list_renders_empty_state(self, client, admin_user, monkeypatch):
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        assert response.status_code == 200
        assert b"No new releases" in response.content

    def test_twelve_releases_default_shows_newest_ten(
        self, client, admin_user, monkeypatch
    ):
        releases = [_mk_release(f"v{i}") for i in range(12, 0, -1)]  # v12..v1
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        assert response.status_code == 200
        body = response.content.decode()
        # 10 newest tags visible
        for tag in [f"v{i}" for i in range(12, 2, -1)]:
            assert f"gh-row-{tag}-qemux86-64" in body
        # v2 and v1 hidden
        assert "gh-row-v2-qemux86-64" not in body
        assert "gh-row-v1-qemux86-64" not in body

    def test_show_all_widens_to_full_list(self, client, admin_user, monkeypatch):
        releases = [_mk_release(f"v{i}") for i in range(12, 0, -1)]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial") + "?show=all")
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64" in body
        assert "gh-row-v12-qemux86-64" in body

    def test_imported_machine_row_is_omitted(
        self, client, admin_user, monkeypatch
    ):
        ImageRelease.objects.create(
            tag="v1", machine="qemux86-64",
            s3_key="x", sha256="a" * 64, size_bytes=1,
        )
        releases = [_mk_release("v1")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64" not in body  # imported → omitted
        assert "gh-row-v1-raspberrypi4-64" in body  # missing → visible

    def test_tag_fully_imported_is_omitted_entirely(
        self, client, admin_user, monkeypatch
    ):
        for m in ("qemux86-64", "raspberrypi4-64"):
            ImageRelease.objects.create(
                tag="v1", machine=m, s3_key=f"x-{m}",
                sha256="a" * 64, size_bytes=1,
            )
        releases = [_mk_release("v1"), _mk_release("v2")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        assert "gh-row-v1-" not in body
        assert "gh-row-v2-qemux86-64" in body

    def test_pending_job_shows_queued_state(
        self, client, admin_user, monkeypatch
    ):
        ImageImportJob.objects.create(
            tag="v1", machine="qemux86-64",
            status=ImageImportJob.Status.PENDING, mark_as_latest=False,
        )
        releases = [_mk_release("v1")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64" in body
        # Queued-row contains the QUEUED pill, no submit button
        row_start = body.index("gh-row-v1-qemux86-64")
        row_end = body.index("</tr>", row_start)
        row = body[row_start:row_end]
        assert "QUEUED" in row
        assert "hx-post" not in row

    def test_missing_asset_disables_queue(
        self, client, admin_user, monkeypatch
    ):
        rel = _mk_release("v1", machines=("qemux86-64",))  # no rpi assets
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [rel],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        rpi_row_start = body.index("gh-row-v1-raspberrypi4-64")
        rpi_row_end = body.index("</tr>", rpi_row_start)
        rpi_row = body[rpi_row_start:rpi_row_end]
        assert "no asset" in rpi_row
        assert "disabled" in rpi_row

    def test_is_latest_renders_pill(self, client, admin_user, monkeypatch):
        rel = _mk_release("v1", is_latest=True)
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [rel],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        row_start = body.index("gh-row-v1-qemux86-64")
        row_end = body.index("</tr>", row_start)
        assert "LATEST" in body[row_start:row_end]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_images_github_browser.py::TestGitHubReleasesPartialViewHappy -v`
Expected: 8 failures — the stub view returns empty body, all assertions fail.

- [ ] **Step 3: Implement the view**

Modify `apps/images/views.py`. Replace the `GitHubReleasesPartialView` stub with:

```python
from enum import Enum

from django.conf import settings
from django.shortcuts import render
from django.http import HttpResponse, HttpResponseBadRequest

from . import github_releases
from .models import ImageImportJob, ImageRelease

MACHINES = [ImageRelease.Machine.QEMU, ImageRelease.Machine.RPI]
NEWEST_LIMIT = 10
ALL_LIMIT = 30


class _RowState(str, Enum):
    READY = "ready"
    QUEUED = "queued"
    NO_ASSET = "no_asset"


class GitHubReleasesPartialView(AdminRequiredMixin, View):
    def get(self, request):
        show = request.GET.get("show", "newest")
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
                status__in=[
                    ImageImportJob.Status.PENDING,
                    ImageImportJob.Status.RUNNING,
                ]
            ).values_list("tag", "machine")
        )

        rows_by_tag = []
        for rel in releases:
            machine_rows = []
            for m in MACHINES:
                key = (rel.tag, m.value)
                if key in imported:
                    continue
                if key in in_flight:
                    state = _RowState.QUEUED.value
                elif not rel.has_assets_for(m.value):
                    state = _RowState.NO_ASSET.value
                else:
                    state = _RowState.READY.value
                machine_rows.append((m.value, state))
            if machine_rows:
                rows_by_tag.append((rel, machine_rows))

        if show != "all":
            rows_by_tag = rows_by_tag[:NEWEST_LIMIT]

        return render(
            request,
            "images/_github_releases_table.html",
            {"rows_by_tag": rows_by_tag, "show": show},
        )
```

(Keep `QuickQueueView` as a stub for now; Task 8 implements it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 24 passed (16 + 8).

- [ ] **Step 5: Commit**

```bash
git add apps/images/views.py tests/test_images_github_browser.py
git commit -m "feat(images): GitHubReleasesPartialView happy paths"
```

---

# Task 7: `GitHubReleasesPartialView` — error path

**Files:**
- Modify: `tests/test_images_github_browser.py`
- (View error-branch already implemented in Task 6; we lock it down.)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_images_github_browser.py`:

```python
@pytest.mark.django_db
class TestGitHubReleasesPartialViewErrors:
    def test_github_api_error_renders_error_partial(
        self, client, admin_user, monkeypatch
    ):
        def boom(repo, limit):
            raise github_releases.GitHubAPIError("read timed out")

        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases", boom
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "GitHub temporarily unreachable" in body
        assert "read timed out" in body
        assert "Try again" in body
```

- [ ] **Step 2: Run the test (should pass — implementation already in place)**

Run: `pytest tests/test_images_github_browser.py::TestGitHubReleasesPartialViewErrors -v`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_images_github_browser.py
git commit -m "test(images): GitHubReleasesPartialView error rendering"
```

---

# Task 8: `QuickQueueView` — core behaviour

**Files:**
- Modify: `apps/images/views.py`
- Modify: `tests/test_images_github_browser.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_images_github_browser.py`:

```python
@pytest.mark.django_db
class TestQuickQueueViewCore:
    def test_creates_job_with_is_latest_true(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "1"},
        )
        assert response.status_code == 200
        job = ImageImportJob.objects.get()
        assert job.tag == "v1"
        assert job.machine == "qemux86-64"
        assert job.mark_as_latest is True
        assert job.status == ImageImportJob.Status.PENDING
        assert job.requested_by == admin_user
        assert b"QUEUED" in response.content
        assert b"gh-row-v1-qemux86-64" in response.content

    def test_creates_job_with_is_latest_false(self, client, admin_user):
        client.force_login(admin_user)
        client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "raspberrypi4-64", "is_latest": "0"},
        )
        job = ImageImportJob.objects.get()
        assert job.mark_as_latest is False

    def test_omitted_is_latest_defaults_false(self, client, admin_user):
        client.force_login(admin_user)
        client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64"},
        )
        job = ImageImportJob.objects.get()
        assert job.mark_as_latest is False

    def test_existing_release_does_not_create_job_shows_imported(
        self, client, admin_user
    ):
        ImageRelease.objects.create(
            tag="v1", machine="qemux86-64",
            s3_key="x", sha256="a" * 64, size_bytes=1,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert response.status_code == 200
        assert ImageImportJob.objects.count() == 0
        assert b"IMPORTED" in response.content

    def test_pending_job_does_not_create_second_shows_queued(
        self, client, admin_user
    ):
        ImageImportJob.objects.create(
            tag="v1", machine="qemux86-64",
            status=ImageImportJob.Status.PENDING, mark_as_latest=False,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert ImageImportJob.objects.count() == 1
        assert b"QUEUED" in response.content

    def test_running_job_does_not_create_second_shows_queued(
        self, client, admin_user
    ):
        ImageImportJob.objects.create(
            tag="v1", machine="qemux86-64",
            status=ImageImportJob.Status.RUNNING, mark_as_latest=False,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert ImageImportJob.objects.count() == 1
        assert b"QUEUED" in response.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_images_github_browser.py::TestQuickQueueViewCore -v`
Expected: 6 failures — stub returns empty body, no DB rows created.

- [ ] **Step 3: Implement `QuickQueueView`**

Modify `apps/images/views.py`. Replace the `QuickQueueView` stub with:

```python
class QuickQueueView(AdminRequiredMixin, View):
    def post(self, request):
        tag = request.POST.get("tag", "").strip()
        machine = request.POST.get("machine", "").strip()
        is_latest = request.POST.get("is_latest", "0") == "1"
        if not tag or machine not in {m.value for m in MACHINES}:
            return HttpResponseBadRequest("invalid tag/machine")

        if ImageRelease.objects.filter(tag=tag, machine=machine).exists():
            return _render_row(
                request, tag, machine, is_latest=is_latest, state="imported"
            )
        existing = ImageImportJob.objects.filter(
            tag=tag, machine=machine,
            status__in=[
                ImageImportJob.Status.PENDING,
                ImageImportJob.Status.RUNNING,
            ],
        ).first()
        if existing:
            return _render_row(
                request, tag, machine, is_latest=is_latest, state="queued"
            )

        ImageImportJob.objects.create(
            tag=tag, machine=machine,
            mark_as_latest=is_latest,
            requested_by=request.user,
        )
        return _render_row(
            request, tag, machine, is_latest=is_latest, state="queued"
        )


def _render_row(request, tag, machine, *, is_latest, state, html_url=""):
    return render(
        request,
        "images/_github_release_row.html",
        {
            "tag": tag,
            "machine": machine,
            "is_latest": is_latest,
            "state": state,
            "html_url": html_url,
        },
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_images_github_browser.py -v`
Expected: 31 passed (24 + 6 new + 1 from Task 7).

- [ ] **Step 5: Commit**

```bash
git add apps/images/views.py tests/test_images_github_browser.py
git commit -m "feat(images): QuickQueueView creates jobs with idempotency"
```

---

# Task 9: `QuickQueueView` — input validation

**Files:**
- Modify: `tests/test_images_github_browser.py`

Validation branch already implemented in Task 8; lock with tests.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_images_github_browser.py`:

```python
@pytest.mark.django_db
class TestQuickQueueViewValidation:
    def test_missing_tag_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"machine": "qemux86-64", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0

    def test_invalid_machine_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "not-a-machine", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0

    def test_empty_tag_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "   ", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/test_images_github_browser.py::TestQuickQueueViewValidation -v`
Expected: 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_images_github_browser.py
git commit -m "test(images): QuickQueueView input validation"
```

---

# Task 10: Wire GitHub panel into `image_list.html` and delete old form

**Files:**
- Modify: `apps/images/templates/images/image_list.html`
- Modify: `apps/images/views.py`
- Modify: `apps/images/urls.py`
- Delete: `apps/images/forms.py`
- Modify: `tests/test_images.py`

This is the user-visible cutover. Old form-rendering context, `ImageImportView`, `ImageImportForm`, and the `import/` URL go away. Existing `TestImportView` tests are removed.

- [ ] **Step 1: Remove `ImageImportForm` import + `import_form` context from `ImageListView`**

In `apps/images/views.py`:

- Delete the import `from .forms import ImageImportForm`.
- In `ImageListView.get_context_data`, delete the line `ctx["import_form"] = ImageImportForm()`.
- Delete the entire `ImageImportView` class.

- [ ] **Step 2: Delete the form module**

```bash
git rm apps/images/forms.py
```

- [ ] **Step 3: Remove the `import/` URL**

In `apps/images/urls.py`, delete the line:

```python
path("import/", views.ImageImportView.as_view(), name="import"),
```

- [ ] **Step 4: Replace the import-form panel in `image_list.html`**

Find the existing `<section class="panel" id="import-form">…</section>` block (the one whose `panel-title` reads "Import from GitHub" and which contains `{{ import_form.tag }}`, `{{ import_form.machine }}`, `{{ import_form.mark_as_latest }}`).

Replace the whole `<section>` (from `<section class="panel" id="import-form">` through the matching `</section>`) with:

```html
{% include "images/_github_panel.html" %}
```

The surrounding `<div class="grid grid-main">` and the sibling `<section>` (Recent import jobs) stay untouched.

- [ ] **Step 5: Remove `TestImportView` from `tests/test_images.py`**

Delete the entire class `TestImportView` (3 test methods: `test_admin_can_create_import_job`, `test_operator_cannot_create_import_job`, `test_anonymous_redirected_to_login`). They reference the now-deleted `images:import` URL.

Also search for any other use of `ImageImportForm`, `reverse("images:import")`, or `ImageImportView` in the test suite and delete those tests. Use:

```bash
grep -nE "ImageImportForm|images:import\b|ImageImportView" tests/
```

Any hit outside `TestImportView` → delete that test or replace the URL with `images:gh_queue` if the intent is generic admin-gate coverage (we already cover that in `TestRoutingAndAuth`).

- [ ] **Step 6: Run the full test suite**

```bash
pytest tests/test_images.py tests/test_images_github_browser.py -v
```

Expected: all tests pass; `TestImportView` no longer collected.

Then run the full suite to catch any other reference:

```bash
pytest -x
```

Expected: green.

- [ ] **Step 7: Smoke-test manually (optional but recommended)**

```bash
python manage.py runserver
```

Open `http://localhost:8000/images/` (after admin login) and verify:
- "Import from GitHub" panel loads.
- Tag links open the correct GitHub release.
- Queue button creates a job; row flips to "QUEUED".
- "Show all" widens the list.
- "Refresh" re-fetches.

- [ ] **Step 8: Commit**

```bash
git add apps/images/views.py apps/images/urls.py \
        apps/images/templates/images/image_list.html \
        tests/test_images.py
git add -u apps/images/forms.py
git commit -m "feat(images): cut over to GitHub release browser, delete manual form"
```

---

# Task 11: Final review pass

**Files:** none (review only)

- [ ] **Step 1: Run the full suite one more time**

```bash
pytest -x
```

Expected: green.

- [ ] **Step 2: Lint / format (project standard)**

```bash
ruff check apps/images/ tests/test_images_github_browser.py
ruff format --check apps/images/ tests/test_images_github_browser.py
```

If any issues, fix them and re-run.

- [ ] **Step 3: `git status` should be clean**

```bash
git status
```

Expected: "nothing to commit, working tree clean" (modulo the pre-existing untracked `uv.lock` / `docs/audit-notes-2026-05-18.md`).

- [ ] **Step 4: Verify branch is ready for review**

```bash
git log --oneline main..HEAD
```

Expected: 10 commits (Task 1 through Task 10).

Done. The feature branch `feature/github-release-browser` is now ready to be opened as a PR.
