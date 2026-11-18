"""Privacy-policy acknowledgement page.

``config.middleware.PrivacyAckMiddleware`` sends every logged-in user whose
``privacy_ack_version`` is below ``settings.PRIVACY_POLICY_VERSION`` here.
The page summarises what is stored and for how long, links the full policy
and records the acknowledgement (user fields + one ``AccountAuditLog`` row).
"""

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import render, resolve_url
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View

from apps.accounts.client_ip import client_ip
from apps.accounts.models import AccountAuditLog, User


def _safe_next(request) -> str:
    """Return the ``next`` target if it is a safe same-host URL, else the dashboard."""
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return resolve_url(settings.LOGIN_REDIRECT_URL)


class PrivacyAckView(View):
    # Part of the login flow: every authenticated user must be able to
    # reach it, whatever their membership level.
    permission_exempt = "auth-flow"
    template_name = "accounts/privacy_ack.html"

    def get(self, request) -> HttpResponse:
        return render(
            request,
            self.template_name,
            {
                "next": _safe_next(request),
                "version": settings.PRIVACY_POLICY_VERSION,
                "policy_url": settings.PRIVACY_POLICY_URL,
            },
        )

    def post(self, request) -> HttpResponseRedirect:
        version = settings.PRIVACY_POLICY_VERSION
        user = request.user
        if version > 0 and user.privacy_ack_version < version:
            now = timezone.now()
            # Conditional UPDATE instead of read-modify-save: two concurrent
            # POSTs (double click, two tabs) can only record — and audit —
            # the acknowledgement once.
            updated = User.objects.filter(pk=user.pk, privacy_ack_version__lt=version).update(
                privacy_ack_version=version, privacy_ack_at=now
            )
            user.privacy_ack_version, user.privacy_ack_at = version, now
            if updated:
                AccountAuditLog.log(
                    event_type=AccountAuditLog.EventType.PRIVACY_ACKNOWLEDGED,
                    actor=user,
                    target_user=user,
                    message=f"privacy policy v{version} acknowledged",
                    ip_address=client_ip(request),
                    details={"version": version},
                )
        return HttpResponseRedirect(_safe_next(request))
