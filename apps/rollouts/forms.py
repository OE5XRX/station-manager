from django import forms
from django.utils.translation import gettext_lazy as _

from apps.stations.models import RolloutGroup


class SequenceAddForm(forms.Form):
    group = forms.ModelChoiceField(queryset=RolloutGroup.objects.none(), label=_("Rollout group"))

    def __init__(self, *args, sequence=None, **kwargs):
        super().__init__(*args, **kwargs)
        if sequence is None:
            self.fields["group"].queryset = RolloutGroup.objects.all()
        else:
            used = sequence.entries.values_list("group_id", flat=True)
            self.fields["group"].queryset = RolloutGroup.objects.exclude(pk__in=used)
