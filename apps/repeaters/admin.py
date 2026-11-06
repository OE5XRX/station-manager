from django.contrib import admin

from .models import Repeater


@admin.register(Repeater)
class RepeaterAdmin(admin.ModelAdmin):
    list_display = ("callsign", "site_name", "output_frequency", "source", "active")
    list_filter = ("source", "active")
    search_fields = ("callsign", "site_name", "locator")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None and obj.source == Repeater.Source.OEVSV:
            return [f.name for f in self.model._meta.fields]
        return ("created_at", "updated_at", "last_synced_at")

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.source == Repeater.Source.OEVSV:
            return False
        return super().has_delete_permission(request, obj)
