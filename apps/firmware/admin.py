from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.firmware.models import FirmwareArtifact, FirmwareDelta


@admin.register(FirmwareArtifact)
class FirmwareArtifactAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "version",
        "artifact_type",
        "target_module",
        "file_size_display",
        "is_stable",
        "uploaded_by",
        "created_at",
    ]
    list_filter = [
        "artifact_type",
        "is_stable",
        "target_module",
        "created_at",
    ]
    search_fields = [
        "name",
        "version",
        "release_notes",
    ]
    readonly_fields = [
        "file_size",
        "checksum_sha256",
        "created_at",
    ]

    @admin.display(description=_("Size"))
    def file_size_display(self, obj):
        return obj.file_size_display


@admin.register(FirmwareDelta)
class FirmwareDeltaAdmin(admin.ModelAdmin):
    list_display = [
        "source_version",
        "target_version",
        "delta_size_display",
        "checksum_sha256",
        "created_at",
    ]
    list_filter = [
        "created_at",
    ]
    search_fields = [
        "source_artifact__name",
        "source_artifact__version",
        "target_artifact__version",
    ]
    readonly_fields = [
        "delta_size",
        "checksum_sha256",
        "created_at",
    ]
    raw_id_fields = [
        "source_artifact",
        "target_artifact",
    ]

    @admin.display(description=_("Source"), ordering="source_artifact__version")
    def source_version(self, obj):
        return str(obj.source_artifact)

    @admin.display(description=_("Target"), ordering="target_artifact__version")
    def target_version(self, obj):
        return str(obj.target_artifact)

    @admin.display(description=_("Delta Size"))
    def delta_size_display(self, obj):
        return obj.delta_size_display
