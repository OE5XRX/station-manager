from django.contrib import admin

from . import services
from .models import Module, ModuleAssignmentHistory, ModuleType


@admin.register(ModuleType)
class ModuleTypeAdmin(admin.ModelAdmin):
    list_display = ("key", "display_name", "hw_repo")
    search_fields = ("key", "display_name")


@admin.register(Module)
class ModuleAdmin(admin.ModelAdmin):
    list_display = ("uid", "module_type", "lifecycle_status", "registration_status",
                    "last_reported_version", "last_seen")
    list_filter = ("module_type", "lifecycle_status", "registration_status", "uid_source")
    search_fields = ("uid",)
    readonly_fields = ("uid", "module_type", "uid_source", "first_seen", "last_seen",
                       "last_reported_version", "created_at", "updated_at")
    actions = ["confirm_registration"]

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
