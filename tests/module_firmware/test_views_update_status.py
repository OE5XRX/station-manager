"""Task 9: update-status pill partial (HTMX self-poll while live)."""

import pytest
from django.urls import reverse

from apps.module_firmware.models import ModuleFirmwareConvergenceState
from tests.module_firmware.update_status_helpers import _cs, _module, _online, _release, _target

State = ModuleFirmwareConvergenceState.State
POLL = 'hx-trigger="every 5s"'


@pytest.fixture
def fm(db):
    from apps.module_firmware.models import ModuleType

    return ModuleType.objects.create(key="fm", display_name="FM")


@pytest.fixture
def base(fm, station):
    _online(station)
    _target(fm)
    rel = _release(fm)
    m = _module(fm, station)
    return station, rel, m


def _url(m, compact=False):
    url = reverse("module_firmware:module_update_status", args=[m.uid])
    return url + ("?compact=1" if compact else "")


@pytest.mark.django_db
def test_partial_polls_only_while_live(client, applicant_user, base):
    station, rel, m = base
    client.force_login(applicant_user)
    resp = client.get(_url(m))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "Update ausstehend" in body
    assert POLL in body
    assert reverse("module_firmware:module_update_status", args=[m.uid]) in body

    m.last_reported_version = rel.version
    m.save(update_fields=["last_reported_version"])
    body = client.get(_url(m)).content.decode()
    assert "Aktuell" in body
    assert POLL not in body
    assert "hx-get" not in body


@pytest.mark.django_db
def test_partial_compact_passes_through(client, applicant_user, base):
    station, rel, m = base
    _cs(m, rel, phase="flashing")
    client.force_login(applicant_user)
    body = client.get(_url(m, compact=True)).content.decode()
    assert "Update läuft: Flashen" in body
    # The polled URL keeps the compact flag so the swap renders the same shape.
    assert "compact=1" in body
    assert POLL in body


@pytest.mark.django_db
def test_partial_failed_does_not_poll(client, applicant_user, base):
    station, rel, m = base
    _cs(m, rel, state=State.FAILED, last_error_message="dfu-util timeout")
    client.force_login(applicant_user)
    body = client.get(_url(m)).content.decode()
    assert "Fehler: dfu-util timeout" in body
    assert POLL not in body


@pytest.mark.django_db
def test_partial_unknown_uid_404(client, applicant_user, db):
    client.force_login(applicant_user)
    url = reverse("module_firmware:module_update_status", args=["NOPE"])
    assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_partial_anonymous_redirects(client, base):
    station, rel, m = base
    resp = client.get(_url(m))
    assert resp.status_code == 302


def test_templates_have_no_multiline_hash_comments():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "check_template_comments.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


CARD_SETTLED = 'hx-trigger="every 30s, fw-status-refresh from:body"'
CARD_LIVE = 'hx-trigger="every 5s, fw-status-refresh from:body"'
# Hidden-tab gate: app.js cancels htmx requests from marked elements while
# document.hidden (no eval'd trigger filter — those fail open under a strict CSP).
VISIBLE_ONLY = "data-poll-visible-only"


@pytest.mark.django_db
def test_partial_card_mode_always_polls(client, applicant_user, base):
    station, rel, m = base
    client.force_login(applicant_user)
    url = reverse("module_firmware:module_update_status", args=[m.uid]) + "?compact=1&card=1"
    body = client.get(url).content.decode()  # pending -> live
    assert CARD_LIVE in body
    assert "?compact=1&amp;card=1" in body
    m.last_reported_version = rel.version
    m.save(update_fields=["last_reported_version"])
    body = client.get(url).content.decode()  # current -> settled, still slow-polls
    assert "Aktuell" in body
    assert CARD_SETTLED in body
    assert "hx-get" in body


@pytest.mark.django_db
def test_partial_non_card_settled_does_not_poll(client, applicant_user, base):
    station, rel, m = base
    m.last_reported_version = rel.version
    m.save(update_fields=["last_reported_version"])
    client.force_login(applicant_user)
    body = client.get(_url(m, compact=True)).content.decode()
    assert "hx-get" not in body and "fw-status-refresh" not in body


@pytest.mark.django_db
def test_partial_htmx_anonymous_stops_polling(client, base):
    """Session expired mid-poll: answer 286 (htmx: stop polling) instead of
    redirecting, so the login page never gets swapped into the pill."""
    station, rel, m = base
    resp = client.get(_url(m, compact=True), HTTP_HX_REQUEST="true")
    assert resp.status_code == 286
    assert resp.content == b""


@pytest.mark.django_db
def test_polling_pills_carry_visibility_marker_and_no_eval_filter(client, applicant_user, base):
    """Card and detail polls are both gated by app.js while the tab is hidden;
    no `[...]` trigger filter (htmx evals those via Function())."""
    station, rel, m = base  # pending -> live
    client.force_login(applicant_user)
    card = reverse("module_firmware:module_update_status", args=[m.uid]) + "?compact=1&card=1"
    for url in (_url(m), _url(m, compact=True), card):
        body = client.get(url).content.decode()
        assert VISIBLE_ONLY in body, url
        assert "document.hidden" not in body, url
    m.last_reported_version = rel.version
    m.save(update_fields=["last_reported_version"])
    assert VISIBLE_ONLY in client.get(card).content.decode()  # settled card still polls
    assert VISIBLE_ONLY not in client.get(_url(m)).content.decode()  # settled detail: no poll


RETRY = "Erneut versuchen"


@pytest.mark.django_db
def test_partial_failed_shows_retry_form_to_staff(client, staff_user, base):
    """The detail pill swaps itself: once polling lands on failed, the retry
    form must be in the fragment (with its own CSRF token), not only on the
    initial page."""
    station, rel, m = base
    _cs(m, rel, state=State.FAILED, last_error_message="dfu-util timeout")
    client.force_login(staff_user)
    body = client.get(_url(m), HTTP_HX_REQUEST="true").content.decode()
    assert body.count(RETRY) == 1
    assert reverse("module_firmware:module_retry_update", args=[m.uid]) in body
    assert f'data-confirm="Update für {m.uid} erneut versuchen?"' in body
    assert 'name="csrfmiddlewaretoken"' in body
    # compact (list/card) never carries the form
    assert RETRY not in client.get(_url(m, compact=True)).content.decode()


@pytest.mark.django_db
def test_partial_failed_hides_retry_form_from_applicant(client, applicant_user, base):
    station, rel, m = base
    _cs(m, rel, state=State.FAILED, last_error_message="dfu-util timeout")
    client.force_login(applicant_user)
    body = client.get(_url(m)).content.decode()
    assert "Fehler: dfu-util timeout" in body
    assert RETRY not in body


@pytest.mark.django_db
def test_partial_retry_form_only_when_failed(client, staff_user, base):
    station, rel, m = base  # pending
    client.force_login(staff_user)
    assert RETRY not in client.get(_url(m)).content.decode()


@pytest.mark.django_db
def test_partial_root_is_block_for_detail_inline_for_compact_and_card(client, staff_user, base):
    """The detail variant can carry block content (retry <div>/<form>), so its
    root must be a <div> — a <span> root would be an invalid content model.
    Compact (list) and card keep the inline <span> root. hx-swap="outerHTML"
    stays on the root in every polling variant."""
    station, rel, m = base
    _cs(m, rel, state=State.FAILED, last_error_message="dfu-util timeout")
    client.force_login(staff_user)
    detail = client.get(_url(m)).content.decode().strip()
    assert detail.startswith('<div class="fw-status"')
    assert detail.endswith("</div>")
    assert RETRY in detail
    compact = client.get(_url(m, compact=True)).content.decode().strip()
    assert compact.startswith('<span class="fw-status fw-status-compact"')
    assert compact.endswith("</span>")
    card_url = reverse("module_firmware:module_update_status", args=[m.uid]) + "?compact=1&card=1"
    card = client.get(card_url).content.decode().strip()
    assert card.startswith('<span class="fw-status fw-status-compact"')
    assert card.endswith("</span>")
    assert 'hx-swap="outerHTML"' in card


@pytest.mark.django_db
def test_partial_live_detail_root_is_div_and_swaps_outer(client, applicant_user, base):
    station, rel, m = base  # pending -> live
    client.force_login(applicant_user)
    body = client.get(_url(m)).content.decode().strip()
    assert body.startswith('<div class="fw-status"')
    assert 'hx-swap="outerHTML"' in body
    assert POLL in body
