from django import forms
from django.utils.translation import gettext_lazy as _

from .models import ImageRelease


class ImageImportForm(forms.Form):
    tag = forms.CharField(
        label=_("Tag"),
        max_length=64,
        help_text=_("GitHub release tag, e.g. v1-alpha"),
    )
    machine = forms.ChoiceField(
        label=_("Machine"),
        choices=ImageRelease.Machine.choices,
    )
    mark_as_latest = forms.BooleanField(
        label=_("Mark as latest for this machine"),
        required=False,
        initial=True,
    )
