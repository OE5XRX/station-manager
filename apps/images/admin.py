from django.contrib import admin

from .models import ImageImportJob, ImageRelease


@admin.register(ImageRelease)
class ImageReleaseAdmin(admin.ModelAdmin):
    list_display = ("tag", "machine", "is_latest", "size_bytes", "imported_at", "imported_by")
    list_filter = ("machine", "is_latest")
    search_fields = ("tag",)
    readonly_fields = (
        "imported_at",
        "imported_by",
        "sha256",
        "s3_key",
        "cosign_bundle_s3_key",
        "size_bytes",
    )


@admin.register(ImageImportJob)
class ImageImportJobAdmin(admin.ModelAdmin):
    list_display = ("tag", "machine", "status", "created_at", "requested_by")
    list_filter = ("status", "machine")
    readonly_fields = (
        "tag",
        "machine",
        "status",
        "created_at",
        "completed_at",
        "image_release",
        "requested_by",
        "error_message",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
