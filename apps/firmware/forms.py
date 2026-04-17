from django import forms
from django.utils.translation import gettext_lazy as _

from apps.firmware.models import FirmwareArtifact


class FirmwareArtifactForm(forms.ModelForm):
    class Meta:
        model = FirmwareArtifact
        fields = [
            "name",
            "version",
            "artifact_type",
            "target_module",
            "file",
            "release_notes",
            "is_stable",
            "compatible_hw_revisions",
        ]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": _("e.g. OE5XRX OS Image"),
                }
            ),
            "version": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": _("e.g. 1.2.3"),
                }
            ),
            "artifact_type": forms.Select(attrs={"class": "form-select"}),
            "target_module": forms.Select(attrs={"class": "form-select"}),
            "file": forms.ClearableFileInput(
                attrs={
                    "class": "form-control",
                    "accept": ".bin,.img,.img.gz,.img.xz,.img.zst,.dfu,.elf,.hex,.fw,.mender",
                }
            ),
            "release_notes": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 5,
                    "placeholder": _("Describe changes in this release..."),
                }
            ),
            "is_stable": forms.CheckboxInput(attrs={"class": "form-check-input"}),
            "compatible_hw_revisions": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": _("e.g. rev1.0, rev1.1, rev2.0"),
                }
            ),
        }


class FirmwareArtifactUpdateForm(FirmwareArtifactForm):
    """Form for editing firmware metadata (file field excluded)."""

    class Meta(FirmwareArtifactForm.Meta):
        exclude = [
            "file",
            "file_size",
            "checksum_sha256",
        ]
        fields = [
            "name",
            "version",
            "artifact_type",
            "target_module",
            "release_notes",
            "is_stable",
            "compatible_hw_revisions",
        ]
