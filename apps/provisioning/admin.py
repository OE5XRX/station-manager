from django.contrib import admin

from .models import ProvisioningJob


@admin.register(ProvisioningJob)
class ProvisioningJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "station",
        "image_release",
        "status",
        "created_at",
        "requested_by",
    )
    list_filter = ("status",)
    readonly_fields = tuple(
        f.name for f in ProvisioningJob._meta.get_fields() if not f.many_to_many
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
