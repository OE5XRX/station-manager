from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import can

from .models import Region, RolloutGroup, Station, StationLogEntry, StationPhoto, StationTag


class StationForm(forms.ModelForm):
    """Form for creating and editing stations."""

    class Meta:
        model = Station
        fields = (
            "name",
            "callsign",
            "description",
            "location_name",
            "latitude",
            "longitude",
            "altitude",
            "hardware_revision",
            "tx_audio_ceiling_dbfs",
            "tags",
            "rollout_group",
            "notes",
        )
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "callsign": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "e.g. OE5XRX"}
            ),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
            "location_name": forms.TextInput(attrs={"class": "form-control"}),
            "latitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001"}),
            "longitude": forms.NumberInput(attrs={"class": "form-control", "step": "0.000001"}),
            "altitude": forms.NumberInput(attrs={"class": "form-control"}),
            "hardware_revision": forms.TextInput(attrs={"class": "form-control"}),
            "tx_audio_ceiling_dbfs": forms.NumberInput(
                attrs={"class": "form-control", "step": "0.5", "lang": "en"}
            ),
            "tags": forms.CheckboxSelectMultiple(),
            "rollout_group": forms.Select(attrs={"class": "form-select"}),
            "notes": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        # RF-safety calibration: only Vereins-Staff/Admin may change it.
        if not can(user, "stations", "manage"):
            self.fields.pop("tx_audio_ceiling_dbfs", None)
        if "rollout_group" in self.fields:
            self.fields["rollout_group"].empty_label = _("No group (release channel)")


class StationPhotoForm(forms.ModelForm):
    """Form for uploading station photos."""

    class Meta:
        model = StationPhoto
        fields = ("image", "caption")
        widgets = {
            "image": forms.ClearableFileInput(attrs={"class": "form-control"}),
            "caption": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": _("Optional caption"),
                }
            ),
        }


class StationLogEntryForm(forms.ModelForm):
    """Form for adding station log entries."""

    class Meta:
        model = StationLogEntry
        fields = ("entry_type", "title", "message")
        widgets = {
            "entry_type": forms.Select(attrs={"class": "form-select"}),
            "title": forms.TextInput(attrs={"class": "form-control"}),
            "message": forms.Textarea(attrs={"class": "form-control", "rows": 4}),
        }


class StationTagForm(forms.ModelForm):
    """Form for creating and editing station tags."""

    class Meta:
        model = StationTag
        fields = ("name", "slug", "color", "description")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "color": forms.TextInput(
                attrs={"class": "form-control form-control-color", "type": "color"}
            ),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }


class RolloutGroupForm(forms.ModelForm):
    """Form for creating and editing rollout groups."""

    class Meta:
        model = RolloutGroup
        fields = ("name", "slug", "channel", "description")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "channel": forms.Select(attrs={"class": "form-select"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            # Fixed after create (disabled fields ignore POSTed values): a
            # channel flip would silently retarget every member.
            self.fields["channel"].disabled = True


class RegionForm(forms.ModelForm):
    class Meta:
        model = Region
        fields = ("name", "slug", "description")
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "slug": forms.TextInput(attrs={"class": "form-control"}),
            "description": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
