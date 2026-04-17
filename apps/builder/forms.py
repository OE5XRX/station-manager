from django import forms
from django.utils.translation import gettext_lazy as _

from apps.builder.models import BuildConfig


class BuildConfigForm(forms.ModelForm):
    class Meta:
        model = BuildConfig
        fields = [
            "name",
            "description",
            "station",
            "tag",
            "base_image",
            "extra_firmware",
            "custom_config",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": _("e.g. OE5XRX Hilltop Build"),
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": _("Optional description of this build config..."),
                }
            ),
            "station": forms.Select(attrs={"class": "form-select"}),
            "tag": forms.Select(attrs={"class": "form-select"}),
            "base_image": forms.Select(attrs={"class": "form-select"}),
            "extra_firmware": forms.CheckboxSelectMultiple(),
            "custom_config": forms.Textarea(
                attrs={
                    "class": "form-control font-monospace",
                    "rows": 6,
                    "placeholder": '{"key": "value"}',
                }
            ),
        }
