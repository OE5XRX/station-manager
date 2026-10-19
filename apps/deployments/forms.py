from django import forms
from django.utils.translation import gettext_lazy as _

from apps.deployments.models import Deployment


class DeploymentForm(forms.ModelForm):
    class Meta:
        model = Deployment
        fields = [
            "image_release",
            "target_type",
            "target_group",
            "target_station",
            "strategy",
            "phase_config",
        ]
        widgets = {
            "image_release": forms.Select(attrs={"class": "form-select"}),
            "target_type": forms.Select(attrs={"class": "form-select"}),
            "target_group": forms.Select(attrs={"class": "form-select"}),
            "target_station": forms.Select(attrs={"class": "form-select"}),
            "strategy": forms.Select(attrs={"class": "form-select"}),
            "phase_config": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 3,
                    "placeholder": _('e.g. {"batch_size": 2, "delay_seconds": 3600}'),
                }
            ),
        }

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

        return cleaned_data
