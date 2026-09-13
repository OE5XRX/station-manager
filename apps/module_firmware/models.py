from django.db import models
from django.utils.translation import gettext_lazy as _


class ModuleType(models.Model):
    """Registry of flashable module types (only firmware-bearing types)."""

    key = models.SlugField(_("key"), unique=True, help_text=_("z. B. fm, power, device-tester"))
    display_name = models.CharField(_("display name"), max_length=128)
    hw_repo = models.CharField(_("hardware repo"), max_length=200, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("module type")
        verbose_name_plural = _("module types")
        ordering = ["key"]

    def __str__(self):
        return self.display_name or self.key
