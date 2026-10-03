"""Write-surface permission matrix tests for StationLogEntry + StationPhoto.

TDD: run RED first (router/viewsets not yet registered), then implement and
verify GREEN.
"""

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Minimal 1×1 RGB PNG (valid image — avoids ImageField validation failures)
# Generated via: struct+zlib, verified against Pillow.
# ---------------------------------------------------------------------------
TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _tiny_image(name="photo.png"):
    return SimpleUploadedFile(name, TINY_PNG, content_type="image/png")


# ===========================================================================
# StationLogEntry
# ===========================================================================


class TestStationLogEntryCreate:
    def test_station_user_creates_on_in_scope_station(self, api_topology, bearer):
        t = api_topology
        payload = {
            "station": t["station_in"].pk,
            "entry_type": "note",
            "title": "Test Entry",
            "message": "Hello",
        }
        r = bearer(t["station_user"]).post(
            reverse("api:station-log-entry-list"), payload, format="json"
        )
        assert r.status_code == 201

    def test_station_user_cannot_create_on_out_of_scope_station(self, api_topology, bearer):
        t = api_topology
        payload = {
            "station": t["station_out"].pk,
            "entry_type": "note",
            "title": "Test Entry",
            "message": "Hello",
        }
        r = bearer(t["station_user"]).post(
            reverse("api:station-log-entry-list"), payload, format="json"
        )
        assert r.status_code == 403

    def test_created_by_is_server_side(self, api_topology, bearer):
        from apps.stations.models import StationLogEntry

        t = api_topology
        payload = {
            "station": t["station_in"].pk,
            "entry_type": "note",
            "title": "Test Entry",
            "message": "Hello",
            "created_by": t["admin"].pk,  # attacker-supplied, must be ignored
        }
        r = bearer(t["station_user"]).post(
            reverse("api:station-log-entry-list"), payload, format="json"
        )
        assert r.status_code == 201
        entry = StationLogEntry.objects.get(pk=r.data["id"])
        assert entry.created_by == t["station_user"]

    def test_applicant_cannot_create(self, api_topology, bearer):
        t = api_topology
        payload = {
            "station": t["station_in"].pk,
            "entry_type": "note",
            "title": "Test Entry",
            "message": "Hello",
        }
        r = bearer(t["applicant"]).post(
            reverse("api:station-log-entry-list"), payload, format="json"
        )
        assert r.status_code == 403

    def test_anon_create_returns_401(self, api_topology, anon_client):
        t = api_topology
        payload = {
            "station": t["station_in"].pk,
            "entry_type": "note",
            "title": "Test Entry",
            "message": "Hello",
        }
        r = anon_client.post(reverse("api:station-log-entry-list"), payload, format="json")
        assert r.status_code == 401

    def test_create_emits_audit_row_with_token_origin(self, api_topology, bearer):
        from apps.stations.models import StationAuditLog

        t = api_topology
        payload = {
            "station": t["station_in"].pk,
            "entry_type": "note",
            "title": "Test Entry",
            "message": "Hello",
        }
        r = bearer(t["station_user"]).post(
            reverse("api:station-log-entry-list"), payload, format="json"
        )
        assert r.status_code == 201
        assert StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.CREATED,
            message__contains="via API token",
        ).exists()


class TestStationLogEntryUpdate:
    def test_out_of_scope_detail_update_returns_404(self, api_topology, bearer):
        from apps.stations.models import StationLogEntry

        t = api_topology
        entry = StationLogEntry.objects.create(
            station=t["station_out"],
            entry_type="note",
            title="Hidden",
            message="secret",
            created_by=t["admin"],
        )
        url = reverse("api:station-log-entry-detail", args=[entry.pk])
        r = bearer(t["station_user"]).patch(url, {"title": "Pwned"}, format="json")
        assert r.status_code == 404

    def test_move_to_out_of_scope_station_returns_403_db_unchanged(self, api_topology, bearer):
        from apps.stations.models import StationLogEntry

        t = api_topology
        entry = StationLogEntry.objects.create(
            station=t["station_in"],
            entry_type="note",
            title="My Entry",
            message="body",
            created_by=t["station_user"],
        )
        url = reverse("api:station-log-entry-detail", args=[entry.pk])
        r = bearer(t["station_user"]).patch(url, {"station": t["station_out"].pk}, format="json")
        assert r.status_code == 403
        entry.refresh_from_db()
        assert entry.station == t["station_in"]  # unchanged

    def test_update_emits_audit_row(self, api_topology, bearer):
        from apps.stations.models import StationAuditLog, StationLogEntry

        t = api_topology
        entry = StationLogEntry.objects.create(
            station=t["station_in"],
            entry_type="note",
            title="My Entry",
            message="body",
            created_by=t["station_user"],
        )
        url = reverse("api:station-log-entry-detail", args=[entry.pk])
        r = bearer(t["station_user"]).patch(url, {"title": "Updated"}, format="json")
        assert r.status_code == 200
        assert StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.UPDATED,
            message__contains="via API token",
        ).exists()


class TestStationLogEntryDelete:
    def test_station_user_can_delete_in_scope(self, api_topology, bearer):
        from apps.stations.models import StationLogEntry

        t = api_topology
        entry = StationLogEntry.objects.create(
            station=t["station_in"],
            entry_type="note",
            title="To Delete",
            message="body",
            created_by=t["station_user"],
        )
        url = reverse("api:station-log-entry-detail", args=[entry.pk])
        r = bearer(t["station_user"]).delete(url)
        assert r.status_code == 204
        assert not StationLogEntry.objects.filter(pk=entry.pk).exists()

    def test_delete_emits_audit_row(self, api_topology, bearer):
        from apps.stations.models import StationAuditLog, StationLogEntry

        t = api_topology
        entry = StationLogEntry.objects.create(
            station=t["station_in"],
            entry_type="note",
            title="To Delete",
            message="body",
            created_by=t["station_user"],
        )
        url = reverse("api:station-log-entry-detail", args=[entry.pk])
        bearer(t["station_user"]).delete(url)
        assert StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.DELETED,
            message__contains="via API token",
        ).exists()

    def test_delete_out_of_scope_returns_404(self, api_topology, bearer):
        from apps.stations.models import StationLogEntry

        t = api_topology
        entry = StationLogEntry.objects.create(
            station=t["station_out"],
            entry_type="note",
            title="Hidden",
            message="secret",
            created_by=t["admin"],
        )
        url = reverse("api:station-log-entry-detail", args=[entry.pk])
        r = bearer(t["station_user"]).delete(url)
        assert r.status_code == 404
        assert StationLogEntry.objects.filter(pk=entry.pk).exists()


# ===========================================================================
# StationPhoto
# ===========================================================================


class TestStationPhotoCreate:
    def test_station_user_uploads_photo_on_in_scope_station(self, api_topology, bearer):
        t = api_topology
        r = bearer(t["station_user"]).post(
            reverse("api:station-photo-list"),
            {
                "station": t["station_in"].pk,
                "caption": "Test photo",
                "image": _tiny_image(),
            },
            format="multipart",
        )
        assert r.status_code == 201

    def test_station_user_cannot_upload_to_out_of_scope_station(self, api_topology, bearer):
        t = api_topology
        r = bearer(t["station_user"]).post(
            reverse("api:station-photo-list"),
            {
                "station": t["station_out"].pk,
                "caption": "Pwned",
                "image": _tiny_image(),
            },
            format="multipart",
        )
        assert r.status_code == 403

    def test_uploaded_by_is_server_side(self, api_topology, bearer):
        from apps.stations.models import StationPhoto

        t = api_topology
        r = bearer(t["station_user"]).post(
            reverse("api:station-photo-list"),
            {
                "station": t["station_in"].pk,
                "caption": "Test",
                "image": _tiny_image(),
                "uploaded_by": t["admin"].pk,  # attacker-supplied, must be ignored
            },
            format="multipart",
        )
        assert r.status_code == 201
        photo = StationPhoto.objects.get(pk=r.data["id"])
        assert photo.uploaded_by == t["station_user"]

    def test_applicant_cannot_upload(self, api_topology, bearer):
        t = api_topology
        r = bearer(t["applicant"]).post(
            reverse("api:station-photo-list"),
            {
                "station": t["station_in"].pk,
                "caption": "Test",
                "image": _tiny_image(),
            },
            format="multipart",
        )
        assert r.status_code == 403

    def test_anon_upload_returns_401(self, api_topology, anon_client):
        t = api_topology
        r = anon_client.post(
            reverse("api:station-photo-list"),
            {
                "station": t["station_in"].pk,
                "caption": "Test",
                "image": _tiny_image(),
            },
            format="multipart",
        )
        assert r.status_code == 401

    def test_create_emits_audit_row_with_token_origin(self, api_topology, bearer):
        from apps.stations.models import StationAuditLog

        t = api_topology
        r = bearer(t["station_user"]).post(
            reverse("api:station-photo-list"),
            {
                "station": t["station_in"].pk,
                "caption": "Test",
                "image": _tiny_image(),
            },
            format="multipart",
        )
        assert r.status_code == 201
        assert StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.CREATED,
            message__contains="via API token",
        ).exists()


class TestStationPhotoUpdate:
    def test_out_of_scope_detail_update_returns_404(self, api_topology, bearer):
        from apps.stations.models import StationPhoto

        t = api_topology
        photo = StationPhoto.objects.create(
            station=t["station_out"],
            image="stations/photos/dummy.png",
            caption="Hidden",
            uploaded_by=t["admin"],
        )
        url = reverse("api:station-photo-detail", args=[photo.pk])
        r = bearer(t["station_user"]).patch(url, {"caption": "Pwned"}, format="json")
        assert r.status_code == 404

    def test_move_to_out_of_scope_station_returns_403_db_unchanged(self, api_topology, bearer):
        from apps.stations.models import StationPhoto

        t = api_topology
        photo = StationPhoto.objects.create(
            station=t["station_in"],
            image="stations/photos/dummy.png",
            caption="My Photo",
            uploaded_by=t["station_user"],
        )
        url = reverse("api:station-photo-detail", args=[photo.pk])
        r = bearer(t["station_user"]).patch(url, {"station": t["station_out"].pk}, format="json")
        assert r.status_code == 403
        photo.refresh_from_db()
        assert photo.station == t["station_in"]  # unchanged

    def test_update_emits_audit_row(self, api_topology, bearer):
        from apps.stations.models import StationAuditLog, StationPhoto

        t = api_topology
        photo = StationPhoto.objects.create(
            station=t["station_in"],
            image="stations/photos/dummy.png",
            caption="My Photo",
            uploaded_by=t["station_user"],
        )
        url = reverse("api:station-photo-detail", args=[photo.pk])
        r = bearer(t["station_user"]).patch(url, {"caption": "Updated"}, format="json")
        assert r.status_code == 200
        assert StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.UPDATED,
            message__contains="via API token",
        ).exists()

    def test_patch_with_new_image_removes_old_blob(self, api_topology, bearer):
        """PATCH with a new image file must delete the old storage blob."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage
        from django.test import TestCase

        from apps.stations.models import StationPhoto

        t = api_topology
        # Upload an initial file directly into default storage
        old_name = default_storage.save(
            "stations/photos/test_old.png",
            ContentFile(TINY_PNG, name="test_old.png"),
        )
        try:
            photo = StationPhoto.objects.create(
                station=t["station_in"],
                image=old_name,
                caption="Original",
                uploaded_by=t["station_user"],
            )
            url = reverse("api:station-photo-detail", args=[photo.pk])
            # captureOnCommitCallbacks forces on_commit hooks to fire even
            # though pytest-django wraps the test in a savepoint that never
            # commits to the real database.
            with TestCase.captureOnCommitCallbacks(execute=True):
                r = bearer(t["station_user"]).patch(
                    url,
                    {"image": _tiny_image("new_photo.png")},
                    format="multipart",
                )
            assert r.status_code == 200
            assert not default_storage.exists(old_name), (
                "Old image blob must be deleted from storage after PATCH with new image"
            )
        finally:
            # Cleanup new blob if the old cleanup somehow failed
            photo.refresh_from_db()
            if photo.image and default_storage.exists(photo.image.name):
                default_storage.delete(photo.image.name)

    def test_patch_without_image_does_not_remove_blob(self, api_topology, bearer):
        """PATCH that does not include `image` must leave the blob intact."""
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        from apps.stations.models import StationPhoto

        t = api_topology
        stored_name = default_storage.save(
            "stations/photos/test_keep.png",
            ContentFile(TINY_PNG, name="test_keep.png"),
        )
        try:
            photo = StationPhoto.objects.create(
                station=t["station_in"],
                image=stored_name,
                caption="Keep me",
                uploaded_by=t["station_user"],
            )
            url = reverse("api:station-photo-detail", args=[photo.pk])
            r = bearer(t["station_user"]).patch(url, {"caption": "Updated caption"}, format="json")
            assert r.status_code == 200
            assert default_storage.exists(stored_name), (
                "Blob must NOT be deleted when image was not included in the PATCH"
            )
        finally:
            default_storage.delete(stored_name)


class TestStationPhotoDelete:
    def test_station_user_can_delete_in_scope(self, api_topology, bearer):
        from apps.stations.models import StationPhoto

        t = api_topology
        photo = StationPhoto.objects.create(
            station=t["station_in"],
            image="stations/photos/dummy.png",
            caption="To Delete",
            uploaded_by=t["station_user"],
        )
        url = reverse("api:station-photo-detail", args=[photo.pk])
        r = bearer(t["station_user"]).delete(url)
        assert r.status_code == 204
        assert not StationPhoto.objects.filter(pk=photo.pk).exists()

    def test_delete_emits_audit_row(self, api_topology, bearer):
        from apps.stations.models import StationAuditLog, StationPhoto

        t = api_topology
        photo = StationPhoto.objects.create(
            station=t["station_in"],
            image="stations/photos/dummy.png",
            caption="To Delete",
            uploaded_by=t["station_user"],
        )
        url = reverse("api:station-photo-detail", args=[photo.pk])
        bearer(t["station_user"]).delete(url)
        assert StationAuditLog.objects.filter(
            station=t["station_in"],
            event_type=StationAuditLog.EventType.DELETED,
            message__contains="via API token",
        ).exists()

    def test_delete_out_of_scope_returns_404(self, api_topology, bearer):
        from apps.stations.models import StationPhoto

        t = api_topology
        photo = StationPhoto.objects.create(
            station=t["station_out"],
            image="stations/photos/dummy.png",
            caption="Hidden",
            uploaded_by=t["admin"],
        )
        url = reverse("api:station-photo-detail", args=[photo.pk])
        r = bearer(t["station_user"]).delete(url)
        assert r.status_code == 404
        assert StationPhoto.objects.filter(pk=photo.pk).exists()
