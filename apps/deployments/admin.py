from django.contrib import admin

from apps.deployments.models import Deployment, DeploymentResult


class DeploymentResultInline(admin.TabularInline):
    model = DeploymentResult
    extra = 0
    readonly_fields = ["station", "status", "started_at", "completed_at", "error_message"]
    can_delete = False


@admin.register(Deployment)
class DeploymentAdmin(admin.ModelAdmin):
    list_display = [
        "__str__",
        "image_release",
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
        "image_release__tag",
        "image_release__machine",
    ]
    readonly_fields = [
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
    readonly_fields = [
        "started_at",
        "completed_at",
    ]
