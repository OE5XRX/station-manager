from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "language", "is_active")
    list_filter = ("language", "is_active", "is_staff", "groups")
    search_fields = ("username", "email", "first_name", "last_name")

    fieldsets = BaseUserAdmin.fieldsets + (
        (_("Station Manager"), {"fields": ("language",)}),
    )

    add_fieldsets = BaseUserAdmin.add_fieldsets + (
        (_("Station Manager"), {"fields": ("language",)}),
    )
