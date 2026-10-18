from django import forms
from django.core.validators import RegexValidator
from django.utils.translation import gettext_lazy as _

from .models import ModuleType

repo_validator = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$",
    _("Use the GitHub form owner/repo, e.g. OE5XRX/FW-RemoteStation."),
)
prefix_validator = RegexValidator(
    r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$",
    _("Letters and digits, optionally joined by single '.', '_' or '-'; no trailing separator."),
)


class ModuleTypeForm(forms.ModelForm):
    class Meta:
        model = ModuleType
        fields = ("key", "display_name", "hw_repo", "firmware_repo", "release_asset_prefix")
        widgets = {
            "key": forms.TextInput(attrs={"class": "form-control"}),
            "display_name": forms.TextInput(attrs={"class": "form-control"}),
            "hw_repo": forms.TextInput(attrs={"class": "form-control"}),
            "firmware_repo": forms.TextInput(attrs={"class": "form-control"}),
            "release_asset_prefix": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("hw_repo", "firmware_repo"):
            self.fields[name].validators.append(repo_validator)
        self.fields["release_asset_prefix"].validators.append(prefix_validator)
        if self.instance and self.instance.pk:
            self.fields["key"].disabled = True
