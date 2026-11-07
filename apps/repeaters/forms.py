from decimal import Decimal, InvalidOperation

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Repeater, ScanList, ScanListEntry

_TONE_HELP = _('CTCSS in Hz (e.g. "88.5") or "none".')


def normalize_tone(raw: str) -> str:
    """CTCSS in Hz (60-260) as plain decimal string, or ``"none"``; ValidationError otherwise."""
    raw = (raw or "").strip().lower()
    if raw in ("", "none"):
        return "none"
    try:
        hz = Decimal(raw)
    except InvalidOperation:
        hz = None
    if hz is None or not hz.is_finite() or not Decimal("60") <= hz <= Decimal("260"):
        raise forms.ValidationError(_('Enter a CTCSS tone in Hz (60-260, e.g. "88.5") or "none".'))
    return format(hz.normalize(), "f")


def _number(**attrs):
    # lang="en": de_AT locales would treat a comma as decimal separator.
    return forms.NumberInput(attrs={"class": "form-control", "lang": "en", **attrs})


class RepeaterForm(forms.ModelForm):
    replaces = forms.ModelChoiceField(
        label=_("Replaces ÖVSV entry"),
        queryset=Repeater.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-control"}),
        help_text=_("Hides the selected ÖVSV entry while this one is active."),
    )

    class Meta:
        model = Repeater
        fields = [
            "callsign",
            "site_name",
            "locator",
            "output_frequency",
            "input_frequency",
            "input_tone",
            "output_tone",
            "bandwidth",
            "latitude",
            "longitude",
            "replaces",
            "comment",
        ]
        widgets = {
            "callsign": forms.TextInput(attrs={"class": "form-control", "autofocus": True}),
            "site_name": forms.TextInput(attrs={"class": "form-control"}),
            "locator": forms.TextInput(attrs={"class": "form-control"}),
            "output_frequency": _number(step="0.00001", min="0.00001"),
            "input_frequency": _number(step="0.00001", min="0.00001"),
            "input_tone": forms.TextInput(attrs={"class": "form-control"}),
            "output_tone": forms.TextInput(attrs={"class": "form-control"}),
            "bandwidth": forms.Select(attrs={"class": "form-control"}),
            "latitude": _number(step="0.000001", min="-90", max="90"),
            "longitude": _number(step="0.000001", min="-180", max="180"),
            "comment": forms.Textarea(attrs={"class": "form-control", "rows": 3}),
        }
        help_texts = {
            "output_frequency": _("Repeater output = what the station receives (MHz)."),
            "input_frequency": _("Repeater input = what the station transmits (MHz)."),
            "input_tone": _TONE_HELP,
            "output_tone": _TONE_HELP,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Repeater.objects.filter(source=Repeater.Source.OEVSV)
        self.fields["replaces"].queryset = qs
        self.fields["replaces"].label_from_instance = lambda r: (
            f"{r.callsign} {r.output_frequency} MHz"
        )

    def _positive(self, name):
        value = self.cleaned_data.get(name)
        if value is not None and value <= 0:
            raise forms.ValidationError(_("Must be greater than zero."))
        return value

    def clean_output_frequency(self):
        return self._positive("output_frequency")

    def clean_input_frequency(self):
        return self._positive("input_frequency")

    def _tone(self, name):
        return normalize_tone(self.cleaned_data.get(name))

    def clean_input_tone(self):
        return self._tone("input_tone")

    def clean_output_tone(self):
        return self._tone("output_tone")

    def clean_latitude(self):
        value = self.cleaned_data.get("latitude")
        if value is not None and not -90 <= value <= 90:
            raise forms.ValidationError(_("Latitude must be between -90 and 90."))
        return value

    def clean_longitude(self):
        value = self.cleaned_data.get("longitude")
        if value is not None and not -180 <= value <= 180:
            raise forms.ValidationError(_("Longitude must be between -180 and 180."))
        return value

    def clean(self):
        cleaned = super().clean()
        if (cleaned.get("latitude") is None) != (cleaned.get("longitude") is None):
            raise forms.ValidationError(_("Enter both latitude and longitude, or neither."))
        return cleaned


class ScanListForm(forms.ModelForm):
    class Meta:
        model = ScanList
        fields = ["name"]
        widgets = {"name": forms.TextInput(attrs={"class": "form-control", "autofocus": True})}

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError(_("Enter a name."))
        return name


class ScanListEntryForm(forms.ModelForm):
    """One entry: a repeater reference *or* a free (simplex) frequency."""

    repeater = forms.ModelChoiceField(
        label=_("Repeater"),
        queryset=Repeater.objects.none(),
        required=False,
        widget=forms.Select(attrs={"class": "form-control"}),
    )

    class Meta:
        model = ScanListEntry
        fields = ["repeater", "frequency", "tone", "label"]
        widgets = {
            "frequency": _number(step="0.00001", min="0.00001"),
            "tone": forms.TextInput(attrs={"class": "form-control"}),
            "label": forms.TextInput(attrs={"class": "form-control", "maxlength": 64}),
        }
        help_texts = {"tone": _TONE_HELP}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["repeater"].queryset = Repeater.objects.visible()
        self.fields["repeater"].label_from_instance = lambda r: (
            f"{r.callsign} {r.output_frequency.normalize():f} MHz"
            + (f" ({r.site_name})" if r.site_name else "")
        )
        self.fields["tone"].required = False

    def clean_tone(self):
        return normalize_tone(self.cleaned_data.get("tone"))

    def clean_frequency(self):
        value = self.cleaned_data.get("frequency")
        if value is not None and value <= 0:
            raise forms.ValidationError(_("Must be greater than zero."))
        return value

    def clean(self):
        cleaned = super().clean()
        has_repeater = cleaned.get("repeater") is not None
        has_frequency = cleaned.get("frequency") is not None
        if has_repeater == has_frequency and not self.errors:
            raise forms.ValidationError(_("Choose either a repeater or enter a frequency."))
        if has_repeater:
            cleaned["tone"] = "none"
        return cleaned
