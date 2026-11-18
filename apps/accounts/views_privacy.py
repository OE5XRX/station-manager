"""Privacy-policy acknowledgement page (filled in by the next task)."""

from django.http import HttpResponse
from django.views import View


class PrivacyAckView(View):
    permission_exempt = "auth-flow"

    def get(self, request):
        return HttpResponse("")
