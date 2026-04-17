from django.contrib import admin

from apps.builder.models import BuildConfig, BuildJob


@admin.register(BuildConfig)
class BuildConfigAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "station",
        "tag",
        "base_image",
        "created_by",
        "created_at",
    ]
    list_filter = [
        "tag",
        "created_at",
    ]
    search_fields = [
        "name",
        "description",
    ]
    readonly_fields = [
        "created_at",
        "updated_at",
    ]
    filter_horizontal = [
        "extra_firmware",
    ]


@admin.register(BuildJob)
class BuildJobAdmin(admin.ModelAdmin):
    list_display = [
        "pk",
        "build_config",
        "status",
        "output_artifact",
        "created_by",
        "started_at",
        "completed_at",
        "created_at",
    ]
    list_filter = [
        "status",
        "created_at",
    ]
    search_fields = [
        "build_config__name",
    ]
    readonly_fields = [
        "created_at",
    ]
