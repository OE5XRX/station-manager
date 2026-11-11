from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.repeaters.forms import normalize_tone

from .models import (
    AprsSettings,
    PassiveServiceConfig,
    StationPassiveConfig,
    is_valid_igate_callsign,
    normalise_callsign,
)


def _number(**attrs):
    # lang="en": de_AT locales would treat a comma as decimal separator.
    return forms.NumberInput(attrs={"class": "form-control", "lang": "en", **attrs})


class ProfileForm(forms.Form):
    """Radio profile fields shared by the APRS profile and the hand-over profile.

    A blank field is omitted from the stored dict (the module keeps its value).
    """

    PROFILE_KEYS = ("frequency", "squelch", "bandwidth", "rx_tone", "tx_tone", "volume")

    frequency = forms.DecimalField(
        label=_("Frequency (MHz)"),
        required=False,
        min_value=Decimal("100"),
        max_value=Decimal("500"),
        decimal_places=4,
        widget=_number(step="0.0001"),
    )
    squelch = forms.IntegerField(
        label=_("Squelch (0-8)"),
        required=False,
        min_value=0,
        max_value=8,
        widget=_number(step="1"),
    )
    bandwidth = forms.ChoiceField(
        label=_("Bandwidth (kHz)"),
        required=False,
        choices=[("", "—"), ("12.5", "12.5"), ("25", "25")],
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    rx_tone = forms.CharField(
        label=_("RX tone (CTCSS)"),
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "none"}),
    )
    tx_tone = forms.CharField(
        label=_("TX tone (CTCSS)"),
        required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "none"}),
    )
    volume = forms.IntegerField(
        label=_("Volume (1-8)"),
        required=False,
        min_value=1,
        max_value=8,
        widget=_number(step="1"),
    )

    def _load_profile(self, values):
        for key in self.PROFILE_KEYS:
            if key in (values or {}):
                self.initial[key] = values[key]

    def clean_rx_tone(self):
        raw = (self.cleaned_data.get("rx_tone") or "").strip()
        return normalize_tone(raw) if raw else ""

    def clean_tx_tone(self):
        raw = (self.cleaned_data.get("tx_tone") or "").strip()
        return normalize_tone(raw) if raw else ""

    def profile_dict(self) -> dict:
        out = {}
        for key in self.PROFILE_KEYS:
            value = self.cleaned_data.get(key)
            if value in (None, ""):
                continue
            out[key] = float(value) if isinstance(value, Decimal) else value
        return out


class PassiveServiceConfigForm(ProfileForm):
    enabled = forms.BooleanField(label=_("APRS enabled"), required=False)
    idle_minutes = forms.IntegerField(
        label=_("Start after idle (minutes)"), min_value=1, widget=_number(step="1")
    )

    def __init__(self, *args, instance: PassiveServiceConfig, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        self.initial.update(enabled=instance.enabled, idle_minutes=instance.idle_minutes)
        self._load_profile(instance.profile)

    def save(self):
        self.instance.enabled = self.cleaned_data["enabled"]
        self.instance.idle_minutes = self.cleaned_data["idle_minutes"]
        self.instance.profile = self.profile_dict()
        self.instance.save()
        return self.instance


class HandoverProfileForm(ProfileForm):
    def __init__(self, *args, instance, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        self._load_profile(instance.values)

    def save(self):
        self.instance.values = self.profile_dict()
        self.instance.save()
        return self.instance


class AprsSettingsForm(forms.ModelForm):
    class Meta:
        model = AprsSettings
        fields = [
            "aprsis_host",
            "aprsis_port",
            "aprsis_filter",
            "max_gate_age_s",
            "retention_days",
            "beacon_interval_min",
            "beacon_comment",
        ]
        widgets = {
            "aprsis_host": forms.TextInput(attrs={"class": "form-control"}),
            "aprsis_port": _number(min="1", max="65535"),
            "aprsis_filter": forms.TextInput(attrs={"class": "form-control"}),
            "max_gate_age_s": _number(min="1", max="300"),
            "retention_days": _number(min="1"),
            "beacon_interval_min": _number(min="10", max="120"),
            "beacon_comment": forms.TextInput(attrs={"class": "form-control"}),
        }

    def clean_max_gate_age_s(self):
        v = self.cleaned_data["max_gate_age_s"]
        if not 1 <= v <= 300:
            raise forms.ValidationError(_("Enter a value between 1 and 300."))
        return v

    def clean_beacon_interval_min(self):
        v = self.cleaned_data["beacon_interval_min"]
        if not 10 <= v <= 120:
            raise forms.ValidationError(_("Enter a value between 10 and 120."))
        return v


class StationPassiveForm(forms.ModelForm):
    """Per-station opt-in and iGate identity (shown on the station edit page, ``manage`` only)."""

    class Meta:
        model = StationPassiveConfig
        fields = ["enabled", "igate_callsign", "igate_ssid"]
        labels = {
            "enabled": _("Passiv APRS mithören und gaten"),
            "igate_callsign": _("iGate-Rufzeichen"),
            "igate_ssid": _("SSID"),
        }
        widgets = {
            "igate_callsign": forms.TextInput(
                attrs={
                    "class": "form-control mono",
                    "placeholder": "OE5XRX",
                    "maxlength": 6,
                    "autocapitalize": "characters",
                    "spellcheck": "false",
                }
            ),
            # 10 is the usual iGate SSID: a hint only, never filled in for the user.
            "igate_ssid": _number(min="0", max="15", step="1", placeholder="10"),
        }

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("prefix", "passive")
        super().__init__(*args, **kwargs)
        # The model allows blank/NULL (opted out); "enabled" decides in clean().
        self.fields["igate_callsign"].required = False
        self.fields["igate_ssid"].required = False

    def clean_igate_callsign(self):
        return normalise_callsign(self.cleaned_data.get("igate_callsign") or "")

    def clean(self):
        cleaned = super().clean()
        call = cleaned.get("igate_callsign") or ""
        ssid = cleaned.get("igate_ssid")
        if call and not is_valid_igate_callsign(call):
            self.add_error("igate_callsign", _("Ungültiges Rufzeichen (ohne SSID, z. B. OE5XRX)."))
            return cleaned
        if cleaned.get("enabled"):
            if not call and "igate_callsign" not in self.errors:
                self.add_error("igate_callsign", _("Zum Aktivieren ist ein Rufzeichen nötig."))
            if ssid is None and "igate_ssid" not in self.errors:
                self.add_error("igate_ssid", _("Zum Aktivieren ist eine SSID nötig (0–15)."))
        if call and ssid is not None and not self.errors:
            clash = StationPassiveConfig.objects.filter(igate_callsign=call, igate_ssid=ssid)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            other = clash.select_related("station").first()
            if other is not None:
                self.add_error(
                    "igate_ssid",
                    _("%(call)s-%(ssid)s gehört schon zur Station %(station)s.")
                    % {"call": call, "ssid": ssid, "station": other.station.name},
                )
        return cleaned
