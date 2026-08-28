from django import forms

from .models import User


class NotificationChannelForm(forms.ModelForm):
    class Meta:
        model = User
        fields = ["notify_channel"]
        widgets = {"notify_channel": forms.RadioSelect}
