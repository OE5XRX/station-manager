from django.contrib import admin

from apps.deployments.models import Deployment, DeploymentResult


class DeploymentResultInline(admin.TabularInline):
    model = DeploymentResult
    extra = 0
    readonly_fields = [
        "station",
        "image_release",
        "status",
        "started_at",
        "completed_at",
        "error_message",
    ]
    can_delete = False


@admin.register(Deployment)
class DeploymentAdmin(admin.ModelAdmin):
    list_display = [
        "__str__",
        "image_tag",
        "image_channel",
        "target_type",
        "strategy",
        "status",
        "created_by",
        "created_at",
    ]
    list_filter = [
        "status",
        "target_type",
        "strategy",
        "created_at",
    ]
    search_fields = [
        "image_tag",
        "image_channel",
    ]
    readonly_fields = [
        "skipped_stations",
        "created_at",
        "updated_at",
    ]
    inlines = [DeploymentResultInline]


@admin.register(DeploymentResult)
class DeploymentResultAdmin(admin.ModelAdmin):
    list_display = [
        "__str__",
        "deployment",
        "station",
        "status",
        "started_at",
        "completed_at",
    ]
    list_filter = [
        "status",
        "started_at",
    ]
    search_fields = [
        "station__name",
        "station__callsign",
        "error_message",
    ]
    # image_release is what the agent was told to install; never re-pointed by
    # hand. Read-only also keeps the form valid when that release is archived
    # (the default manager hides archived rows from the choice queryset).
    readonly_fields = [
        "image_release",
        "started_at",
        "completed_at",
    ]
