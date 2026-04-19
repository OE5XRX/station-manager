from django.contrib import admin

from .models import RolloutSequence, RolloutSequenceEntry


class RolloutSequenceEntryInline(admin.TabularInline):
    model = RolloutSequenceEntry
    extra = 0
    ordering = ("position",)


@admin.register(RolloutSequence)
class RolloutSequenceAdmin(admin.ModelAdmin):
    inlines = [RolloutSequenceEntryInline]
    readonly_fields = ("created_at", "updated_at", "updated_by")

    def has_add_permission(self, request):
        # Belt to the singleton_key unique index's braces: keep the
        # admin UI from even offering "Add another" once the seeded
        # row exists.
        return not RolloutSequence.objects.exists()

    def has_delete_permission(self, request, obj=None):
        # Deleting the singleton would break every view that calls
        # current_sequence() until the next request re-seeds one.
        return False
