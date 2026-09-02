import http.client
import io
import json
import urllib.error
from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.images import github_releases
from apps.images.github_releases import (
    GitHubAPIError,
    GitHubRelease,
    fetch_releases,
)
from apps.images.models import ImageImportJob, ImageRelease


def _triple(machine, channel, tag):
    """Symmetric asset triple: oe5xrx-<machine>-<channel>-<tag>.wic.bz2 (+sidecars)."""
    base = f"oe5xrx-{machine}-{channel}-{tag}.wic.bz2"
    return {base, f"{base}.bundle", f"{base}.sha256"}


class TestGitHubReleaseAssets:
    def _release(self, names):
        return GitHubRelease(
            tag="v1-alpha",
            html_url="https://example.invalid/v1-alpha",
            is_latest=False,
            asset_names=frozenset(names),
        )

    def test_all_three_assets_present_returns_true(self):
        rel = self._release(_triple("qemux86-64", "release", "v1-alpha"))
        assert rel.has_assets_for("qemux86-64", "release") is True

    def test_missing_bundle_returns_false(self):
        base = "oe5xrx-qemux86-64-release-v1-alpha.wic.bz2"
        rel = self._release([base, f"{base}.sha256"])
        assert rel.has_assets_for("qemux86-64", "release") is False

    def test_missing_sha256_returns_false(self):
        base = "oe5xrx-qemux86-64-release-v1-alpha.wic.bz2"
        rel = self._release([base, f"{base}.bundle"])
        assert rel.has_assets_for("qemux86-64", "release") is False

    def test_missing_wic_returns_false(self):
        base = "oe5xrx-qemux86-64-release-v1-alpha.wic.bz2"
        rel = self._release([f"{base}.bundle", f"{base}.sha256"])
        assert rel.has_assets_for("qemux86-64", "release") is False

    def test_other_machine_assets_dont_satisfy(self):
        rel = self._release(_triple("qemux86-64", "release", "v1-alpha"))
        assert rel.has_assets_for("raspberrypi4-64", "release") is False

    def test_other_channel_assets_dont_satisfy(self):
        rel = self._release(_triple("qemux86-64", "release", "v1-alpha"))
        assert rel.has_assets_for("qemux86-64", "dev") is False

    def test_channels_for_extracts_dev_and_release(self):
        names = _triple("qemux86-64", "release", "v1-alpha") | _triple(
            "qemux86-64", "dev", "v1-alpha"
        )
        assert self._release(names).channels_for("qemux86-64") == frozenset({"release", "dev"})

    def test_channels_for_handles_hyphenated_machine(self):
        # raspberrypi4-64 contains a hyphen; channel must be extracted by
        # prefix/suffix strip, not by splitting on '-'.
        names = _triple("raspberrypi4-64", "dev", "v1-alpha")
        assert self._release(names).channels_for("raspberrypi4-64") == frozenset({"dev"})

    def test_channels_for_ignores_incomplete_triple(self):
        base = "oe5xrx-qemux86-64-dev-v1-alpha.wic.bz2"
        names = {base, f"{base}.sha256"}  # missing .bundle
        assert self._release(names).channels_for("qemux86-64") == frozenset()

    def test_channels_for_empty_when_no_assets(self):
        assert self._release([]).channels_for("qemux86-64") == frozenset()

    def test_channels_for_ignores_invalid_channel_token(self):
        # An uppercase (non-slug) channel token has a complete triple but
        # must not be surfaced — it would render a row QuickQueueView always
        # rejects and could overflow ImageRelease.channel on import.
        names = _triple("qemux86-64", "DEV", "v1-alpha")
        assert self._release(names).channels_for("qemux86-64") == frozenset()

    def test_channels_for_ignores_overlong_channel_token(self):
        overlong = "d" * 33  # > ImageRelease.channel max_length (32)
        names = _triple("qemux86-64", overlong, "v1-alpha")
        assert self._release(names).channels_for("qemux86-64") == frozenset()


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


class TestFetchReleasesHTTPException:
    def test_http_client_exception_is_wrapped_as_github_api_error(self):
        def fake(req, timeout):
            raise http.client.RemoteDisconnected("server closed connection")

        with patch("apps.images.github_releases.urllib.request.urlopen", side_effect=fake):
            with pytest.raises(GitHubAPIError) as exc:
                fetch_releases("OE5XRX/linux-image")
            assert "server closed connection" in str(exc.value)


class TestFetchReleasesErrors:
    def test_list_url_error_raises_github_api_error(self):
        def fake(req, timeout):
            raise urllib.error.URLError("no route to host")

        with patch("apps.images.github_releases.urllib.request.urlopen", side_effect=fake):
            with pytest.raises(GitHubAPIError) as exc:
                fetch_releases("OE5XRX/linux-image")
            assert "no route to host" in str(exc.value)

    def test_list_timeout_raises_github_api_error(self):
        def fake(req, timeout):
            raise TimeoutError("read timed out")

        with patch("apps.images.github_releases.urllib.request.urlopen", side_effect=fake):
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
        list_url = "https://api.github.com/repos/OE5XRX/linux-image/releases?per_page=30"
        latest_url = "https://api.github.com/repos/OE5XRX/linux-image/releases/latest"

        def fake(req, timeout):
            url = req.full_url
            if url == list_url:
                return _gh_response([{"tag_name": "v1", "html_url": "", "assets": []}])
            if url == latest_url:
                raise urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)
            raise AssertionError(f"unexpected url {url}")

        with patch("apps.images.github_releases.urllib.request.urlopen", side_effect=fake):
            result = fetch_releases("OE5XRX/linux-image")

        assert len(result) == 1
        assert result[0].is_latest is False


@pytest.mark.django_db
class TestRoutingAndAuth:
    def test_partial_url_reverses(self):
        # i18n_patterns prefixes URLs with the active language code.
        assert reverse("images:gh_partial") == "/en/images/github-releases/"

    def test_queue_url_reverses(self):
        assert reverse("images:gh_queue") == "/en/images/github-releases/queue/"

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


def _mk_release(
    tag,
    is_latest=False,
    machines=("qemux86-64", "raspberrypi4-64"),
    channels=("release",),
):
    """Build a release with symmetric channel-aware asset names.

    By default each machine gets a complete release-channel triple. Pass
    ``channels`` to add/replace which channels are present per machine.
    """
    names = set()
    for m in machines:
        for channel in channels:
            names.update(_triple(m, channel, tag))
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

    def test_twelve_releases_default_shows_newest_ten(self, client, admin_user, monkeypatch):
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
            assert f"gh-row-{tag}-qemux86-64-release" in body
        # v2 and v1 hidden
        assert "gh-row-v2-qemux86-64-release" not in body
        assert "gh-row-v1-qemux86-64-release" not in body

    def test_show_all_widens_to_full_list(self, client, admin_user, monkeypatch):
        releases = [_mk_release(f"v{i}") for i in range(12, 0, -1)]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial") + "?show=all")
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64-release" in body
        assert "gh-row-v12-qemux86-64-release" in body

    def test_imported_machine_row_is_omitted(self, client, admin_user, monkeypatch):
        ImageRelease.objects.create(
            tag="v1",
            machine="qemux86-64",
            s3_key="x",
            sha256="a" * 64,
            size_bytes=1,
        )
        releases = [_mk_release("v1")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        # qemu/release imported → omitted; rpi/release not imported → visible
        assert "gh-row-v1-qemux86-64-release" not in body
        assert "gh-row-v1-raspberrypi4-64-release" in body

    def test_tag_fully_imported_is_omitted_entirely(self, client, admin_user, monkeypatch):
        for m in ("qemux86-64", "raspberrypi4-64"):
            ImageRelease.objects.create(
                tag="v1",
                machine=m,
                s3_key=f"x-{m}",
                sha256="a" * 64,
                size_bytes=1,
            )
        releases = [_mk_release("v1"), _mk_release("v2")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        # Every (machine, release) combo of v1 is imported → whole tag omitted.
        assert "gh-row-v1-" not in body
        assert "gh-row-v2-qemux86-64-release" in body

    def test_pending_job_shows_queued_state(self, client, admin_user, monkeypatch):
        ImageImportJob.objects.create(
            tag="v1",
            machine="qemux86-64",
            status=ImageImportJob.Status.PENDING,
            mark_as_latest=False,
        )
        releases = [_mk_release("v1")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64-release" in body
        # Queued-row contains the QUEUED pill, no submit button
        row_start = body.index("gh-row-v1-qemux86-64-release")
        row_end = body.index("</tr>", row_start)
        row = body[row_start:row_end]
        assert "QUEUED" in row
        assert "hx-post" not in row

    def test_machine_without_complete_triple_has_no_row(self, client, admin_user, monkeypatch):
        # Only qemu has a complete triple; rpi has none. With dynamic
        # discovery an undiscovered machine/channel simply produces no row
        # (there is no "no asset" disabled state anymore).
        rel = _mk_release("v1", machines=("qemux86-64",))  # no rpi assets
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [rel],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64-release" in body
        assert "gh-row-v1-raspberrypi4-64" not in body

    def test_multiple_channels_render_separate_rows(self, client, admin_user, monkeypatch):
        rel = _mk_release("v1", machines=("qemux86-64",), channels=("release", "dev"))
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [rel],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        assert "gh-row-v1-qemux86-64-release" in body
        assert "gh-row-v1-qemux86-64-dev" in body
        # Non-release channel gets a prominent accent pill labelled DEV.
        dev_start = body.index("gh-row-v1-qemux86-64-dev")
        dev_end = body.index("</tr>", dev_start)
        assert "DEV" in body[dev_start:dev_end]

    def test_is_latest_renders_pill(self, client, admin_user, monkeypatch):
        rel = _mk_release("v1", is_latest=True)
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [rel],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        row_start = body.index("gh-row-v1-qemux86-64-release")
        row_end = body.index("</tr>", row_start)
        assert "LATEST" in body[row_start:row_end]


@pytest.mark.django_db
class TestGitHubReleasesPartialViewErrors:
    def test_github_api_error_renders_error_partial(self, client, admin_user, monkeypatch):
        def boom(repo, limit):
            raise github_releases.GitHubAPIError("read timed out")

        monkeypatch.setattr("apps.images.views.github_releases.fetch_releases", boom)
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        assert response.status_code == 200
        body = response.content.decode()
        assert "GitHub temporarily unreachable" in body
        assert "read timed out" in body
        assert "Try again" in body


@pytest.mark.django_db
class TestQuickQueueViewCore:
    def test_creates_job_with_is_latest_true(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "release", "is_latest": "1"},
        )
        assert response.status_code == 200
        job = ImageImportJob.objects.get()
        assert job.tag == "v1"
        assert job.machine == "qemux86-64"
        assert job.channel == "release"
        assert job.mark_as_latest is True
        assert job.status == ImageImportJob.Status.PENDING
        assert job.requested_by == admin_user
        assert b"QUEUED" in response.content
        assert b"gh-row-v1-qemux86-64-release" in response.content

    def test_creates_job_with_dev_channel(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "dev", "is_latest": "1"},
        )
        assert response.status_code == 200
        job = ImageImportJob.objects.get()
        assert job.channel == "dev"
        assert b"gh-row-v1-qemux86-64-dev" in response.content

    def test_omitted_channel_defaults_release(self, client, admin_user):
        client.force_login(admin_user)
        client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        job = ImageImportJob.objects.get()
        assert job.channel == "release"

    def test_same_tag_machine_different_channel_creates_two_jobs(self, client, admin_user):
        client.force_login(admin_user)
        client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "release"},
        )
        client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "dev"},
        )
        assert ImageImportJob.objects.count() == 2
        assert set(ImageImportJob.objects.values_list("channel", flat=True)) == {"release", "dev"}

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

    def test_existing_release_does_not_create_job_shows_imported(self, client, admin_user):
        ImageRelease.objects.create(
            tag="v1",
            machine="qemux86-64",
            s3_key="x",
            sha256="a" * 64,
            size_bytes=1,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert response.status_code == 200
        assert ImageImportJob.objects.count() == 0
        assert b"IMPORTED" in response.content

    def test_pending_job_does_not_create_second_shows_queued(self, client, admin_user):
        ImageImportJob.objects.create(
            tag="v1",
            machine="qemux86-64",
            status=ImageImportJob.Status.PENDING,
            mark_as_latest=False,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert ImageImportJob.objects.count() == 1
        assert b"QUEUED" in response.content

    def test_running_job_does_not_create_second_shows_queued(self, client, admin_user):
        ImageImportJob.objects.create(
            tag="v1",
            machine="qemux86-64",
            status=ImageImportJob.Status.RUNNING,
            mark_as_latest=False,
        )
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert ImageImportJob.objects.count() == 1
        assert b"QUEUED" in response.content


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

    def test_empty_channel_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "   ", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0

    def test_invalid_channel_slug_uppercase_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "DEV", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0

    def test_invalid_channel_slug_space_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "de v", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0

    def test_overlong_channel_returns_400(self, client, admin_user):
        # A slug that passes the regex but exceeds ImageRelease.channel
        # max_length (32) must 400, not 500 on a DB DataError.
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "d" * 33, "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0

    def test_invalid_channel_slug_slash_returns_400(self, client, admin_user):
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "channel": "dev/x", "is_latest": "0"},
        )
        assert response.status_code == 400
        assert ImageImportJob.objects.count() == 0


@pytest.mark.django_db
class TestDottedTagSelectorRegression:
    """Yocto daily builds use tags like '2026.04.24-18'. The bare id
    selector '#gh-row-2026.04.24-18-…' is parsed by CSS as
    '#gh-row-2026 .04 .24-18-…' (class selectors). hx-target must
    survive this — use an attribute selector instead."""

    def test_dotted_tag_uses_attribute_selector_in_partial(self, client, admin_user, monkeypatch):
        tag = "2026.04.24-18"
        rel = _mk_release(tag)
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [rel],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        # Row is still findable by id (HTML id allows dots, only CSS selector
        # parsing trips up).
        assert f'id="gh-row-{tag}-qemux86-64-release"' in body
        # hx-target must NOT use the bare id selector.
        bad_selector = f'hx-target="#gh-row-{tag}-qemux86-64-release"'
        assert bad_selector not in body, (
            "hx-target uses bare id selector which CSS misparses on dots; "
            "switch to attribute selector"
        )
        # Verify our actual fix is in place.
        assert f'data-gh-row="{tag}-qemux86-64-release"' in body
        assert f"hx-target=\"[data-gh-row='{tag}-qemux86-64-release']\"" in body

    def test_dotted_tag_quick_queue_returns_swappable_row(self, client, admin_user):
        tag = "2026.04.24-18"
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": tag, "machine": "qemux86-64", "channel": "release", "is_latest": "1"},
        )
        assert response.status_code == 200
        body = response.content.decode()
        # The returned row still carries both the id and the data attribute
        # so HTMX outerHTML-swap finds it via the original attribute selector.
        assert f'id="gh-row-{tag}-qemux86-64-release"' in body
        assert f'data-gh-row="{tag}-qemux86-64-release"' in body


@pytest.mark.django_db
class TestShowModePreservation:
    """Refresh button must preserve the current show-mode. Originally
    Refresh always called `/gh_partial/` with no `show=`, which would
    revert a user from 'Show all' back to 'newest 10' on click."""

    def test_show_all_partial_refresh_button_preserves_show_all(
        self, client, admin_user, monkeypatch
    ):
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial") + "?show=all")
        body = response.content.decode()
        # Refresh button in the controls partial must carry ?show=all
        # when current mode is 'all'.
        assert "Refresh" in body
        assert "?show=all" in body

    def test_show_newest_partial_refresh_button_omits_show_param(
        self, client, admin_user, monkeypatch
    ):
        import re

        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: [],
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        # In default mode, the Refresh button must NOT add show=all to its URL.
        # Match the <button…>…Refresh…</button> block and inspect its
        # attributes for the show=all leak.
        m = re.search(
            r"<button[^>]*>\s*[^<]*Refresh[^<]*</button>",
            body,
            re.DOTALL,
        )
        assert m, "Refresh button not found in partial body"
        assert "?show=all" not in m.group(0)

    def test_error_partial_includes_controls(self, client, admin_user, monkeypatch):
        def boom(repo, limit):
            raise github_releases.GitHubAPIError("read timed out")

        monkeypatch.setattr("apps.images.views.github_releases.fetch_releases", boom)
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial") + "?show=all")
        body = response.content.decode()
        # Even in error state, controls render with the right show-mode
        # so the user can switch back without losing context.
        assert "Refresh" in body
        assert "?show=all" in body
        # And the error banner is also shown.
        assert "GitHub temporarily unreachable" in body


@pytest.mark.django_db
class TestArchivedReleaseHandling:
    """Soft-deleted (archived) ImageRelease rows must still count as
    'imported' so the GitHub browser doesn't show them as queueable and
    QuickQueueView doesn't create duplicate jobs for them."""

    def test_archived_release_row_is_omitted_from_partial(self, client, admin_user, monkeypatch):
        release = ImageRelease.objects.create(
            tag="v1",
            machine="qemux86-64",
            s3_key="x",
            sha256="a" * 64,
            size_bytes=1,
        )
        release.archive()  # soft-delete
        # ImageRelease.objects.filter(...).count() is 0 now, but all_objects sees it.
        assert ImageRelease.objects.filter(tag="v1").count() == 0
        assert ImageRelease.all_objects.filter(tag="v1").count() == 1

        releases = [_mk_release("v1")]
        monkeypatch.setattr(
            "apps.images.views.github_releases.fetch_releases",
            lambda repo, limit: releases,
        )
        client.force_login(admin_user)
        response = client.get(reverse("images:gh_partial"))
        body = response.content.decode()
        # Archived (tag,machine,channel) must be treated as imported -> row omitted.
        assert "gh-row-v1-qemux86-64-release" not in body

    def test_archived_release_quick_queue_returns_imported(self, client, admin_user):
        release = ImageRelease.objects.create(
            tag="v1",
            machine="qemux86-64",
            s3_key="x",
            sha256="a" * 64,
            size_bytes=1,
        )
        release.archive()
        client.force_login(admin_user)
        response = client.post(
            reverse("images:gh_queue"),
            {"tag": "v1", "machine": "qemux86-64", "is_latest": "0"},
        )
        assert response.status_code == 200
        assert ImageImportJob.objects.count() == 0
        assert b"IMPORTED" in response.content
