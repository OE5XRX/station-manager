from django import forms
from django.utils.translation import gettext_lazy as _

from apps.deployments.models import Deployment
from apps.images.models import ImageRelease
from apps.stations.models import RolloutGroup, Station


class DeploymentForm(forms.Form):
    """Input for a new deployment.

    ``image_release`` only selects the *version* (its tag + channel); the
    service resolves the image per target station's machine.
    """

    image_release = forms.ModelChoiceField(
        label=_("image release"),
        queryset=ImageRelease.objects.all(),
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    target_type = forms.ChoiceField(
        label=_("target type"),
        choices=Deployment.TargetType.choices,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    target_group = forms.ModelChoiceField(
        label=_("target group"),
        queryset=RolloutGroup.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    target_station = forms.ModelChoiceField(
        label=_("target station"),
        queryset=Station.objects.all(),
        required=False,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    strategy = forms.ChoiceField(
        label=_("strategy"),
        choices=Deployment.Strategy.choices,
        initial=Deployment.Strategy.IMMEDIATE,
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    phase_config = forms.JSONField(
        label=_("phase configuration"),
        required=False,
        initial=dict,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 3,
                "placeholder": _('e.g. {"batch_size": 2, "delay_seconds": 3600}'),
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()
        target_type = cleaned_data.get("target_type")

        if target_type == Deployment.TargetType.GROUP and not cleaned_data.get("target_group"):
            self.add_error(
                "target_group",
                _("A target group is required when target type is 'By Group'."),
            )

        if target_type == Deployment.TargetType.STATION and not cleaned_data.get("target_station"):
            self.add_error(
                "target_station",
                _("A target station is required when target type is 'Single Station'."),
            )

        if cleaned_data.get("phase_config") is None:
            cleaned_data["phase_config"] = {}
        return cleaned_data
