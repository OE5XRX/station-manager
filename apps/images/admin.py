from django.contrib import admin

from .models import ImageRelease


@admin.register(ImageRelease)
class ImageReleaseAdmin(admin.ModelAdmin):
    list_display = ("tag", "machine", "is_latest", "size_bytes", "imported_at", "imported_by")
    list_filter = ("machine", "is_latest")
    search_fields = ("tag",)
    readonly_fields = ("imported_at", "imported_by", "sha256", "s3_key", "cosign_bundle_s3_key", "size_bytes")
