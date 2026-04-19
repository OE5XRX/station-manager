from django import forms
from django.utils.translation import gettext_lazy as _

from apps.stations.models import StationTag


class SequenceAddForm(forms.Form):
    tag = forms.ModelChoiceField(queryset=StationTag.objects.none(), label=_("Tag"))

    def __init__(self, *args, sequence=None, **kwargs):
        super().__init__(*args, **kwargs)
        if sequence is None:
            self.fields["tag"].queryset = StationTag.objects.all()
        else:
            used = sequence.entries.values_list("tag_id", flat=True)
            self.fields["tag"].queryset = StationTag.objects.exclude(pk__in=used)
