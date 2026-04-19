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
