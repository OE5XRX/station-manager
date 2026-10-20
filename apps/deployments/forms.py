from django import forms
from django.utils.translation import gettext_lazy as _

from apps.deployments.models import Deployment
from apps.deployments.services import available_versions, plan_deployment, target_stations
from apps.stations.models import RolloutGroup, Station

VERSION_SEP = "|"


def version_value(channel: str, tag: str) -> str:
    return f"{channel}{VERSION_SEP}{tag}"


class DeploymentForm(forms.Form):
    """Input for a new deployment.

    The operator picks a *version* (``channel|tag``), never a machine-specific
    image: the service resolves each target station's image by its machine.
    ``clean()`` runs that same plan, so the preview, the validation and the
    created deployment agree. After validation ``self.plans`` holds the
    per-station plan (``None`` if version/target could not be resolved).
    """

    version = forms.ChoiceField(
        label=_("version"),
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

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plans = None
        grouped: dict[str, list[tuple[str, str]]] = {}
        for channel, tag, machines in available_versions():
            grouped.setdefault(channel, []).append(
                (version_value(channel, tag), f"{tag} · {', '.join(machines)}")
            )
        self.fields["version"].choices = [("", "---------"), *grouped.items()]

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

        version = cleaned_data.get("version")
        target_ok = target_type and not {"target_type", "target_group", "target_station"} & set(
            self.errors
        )
        if not version or not target_ok:
            return cleaned_data

        # The choice field already guarantees the value is an offered version.
        channel, _sep, tag = version.partition(VERSION_SEP)
        cleaned_data["channel"], cleaned_data["tag"] = channel, tag
        stations = (
            target_stations(
                target_type=target_type,
                target_group=cleaned_data.get("target_group"),
                target_station=cleaned_data.get("target_station"),
            )
            .select_related("current_image_release", "rollout_group")
            .order_by("name")
        )
        self.plans = plan_deployment(tag, channel, stations)
        if not any(p.ok for p in self.plans):
            errors = [
                _("No target station can receive %(tag)s (%(channel)s).")
                % {"tag": tag, "channel": channel}
            ]
            errors += [f"{p.station.name}: {p.message}" for p in self.plans]
            if not self.plans:
                errors = [_("The selected target has no stations.")]
            self.add_error(None, errors)
        return cleaned_data
