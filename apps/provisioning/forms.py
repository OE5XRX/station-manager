from django import forms
from django.utils.translation import gettext_lazy as _

from apps.images.models import ImageRelease


class ProvisioningForm(forms.Form):
    image_release = forms.ModelChoiceField(
        label=_("Image version"),
        queryset=ImageRelease.objects.all(),
    )
