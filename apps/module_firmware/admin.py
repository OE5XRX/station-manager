from django.contrib import admin

from . import services
from .models import (
    Module,
    ModuleAssignmentHistory,
    ModuleFirmwareImportJob,
    ModuleFirmwareRelease,
    ModuleType,
)


@admin.register(ModuleType)
class ModuleTypeAdmin(admin.ModelAdmin):
    list_display = ("key", "display_name", "hw_repo", "firmware_repo", "release_asset_prefix")
    search_fields = ("key", "display_name")


@admin.register(ModuleFirmwareRelease)
class ModuleFirmwareReleaseAdmin(admin.ModelAdmin):
    list_display = (
        "module_type",
        "variant",
        "version",
        "size_bytes",
        "imported_at",
        "archived_at",
    )
    list_filter = ("module_type", "variant", "archived_at")
    search_fields = ("version", "source_tag")
    readonly_fields = (
        "storage_key",
        "sha256",
        "cosign_bundle_key",
        "size_bytes",
        "source_repo",
        "source_tag",
        "source_github_url",
        "imported_at",
        "imported_by",
    )
    actions = ["archive_selected", "restore_selected"]

    def get_queryset(self, request):
        return self.model.all_objects.all()

    @admin.action(description="Archivieren")
    def archive_selected(self, request, queryset):
        for r in queryset:
            r.archive()

    @admin.action(description="Wiederherstellen")
    def restore_selected(self, request, queryset):
        for r in queryset:
            r.restore()


@admin.register(ModuleFirmwareImportJob)
class ModuleFirmwareImportJobAdmin(admin.ModelAdmin):
    list_display = ("module_type", "tag", "status", "created_at", "completed_at")
    list_filter = ("status", "module_type")
    readonly_fields = (
        "status",
        "error_message",
        "release",
        "requested_by",
        "created_at",
        "completed_at",
    )


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = (
        "uid",
        "module_type",
        "lifecycle_status",
        "registration_status",
        "last_reported_version",
        "last_seen",
    )
    list_filter = ("module_type", "lifecycle_status", "registration_status", "uid_source")
    search_fields = ("uid",)
    # lifecycle_status / registration_status are read-only here: editing them via
    # the plain admin form would bypass services.set_lifecycle / confirm_registration
    # and leave no audit trail. Use the bulk action / staff UI instead.
    readonly_fields = (
        "uid",
        "module_type",
        "uid_source",
        "lifecycle_status",
        "registration_status",
        "first_seen",
        "last_seen",
        "last_reported_version",
        "created_at",
        "updated_at",
    )
    actions = ["confirm_registration"]

    def has_add_permission(self, request):
        # Modules are created by inventory ingestion; module_type is required and
        # read-only, so the admin add form can't build a valid row anyway.
        return False

    def has_delete_permission(self, request, obj=None):
        # Deleting a Module CASCADE-drops its whole assignment timeline and nulls
        # its audit attribution. Use the `retired` lifecycle instead.
        return False

    @admin.action(description="Registrierung bestätigen")
    def confirm_registration(self, request, queryset):
        for module in queryset:
            services.confirm_registration(module, user=request.user)


@admin.register(ModuleAssignmentHistory)
class ModuleAssignmentHistoryAdmin(admin.ModelAdmin):
    list_display = ("module", "station", "slot", "from_ts", "to_ts", "reason")
    list_filter = ("reason",)
    search_fields = ("module__uid",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
