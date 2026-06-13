"""Single point of email-dispatch for account-lifecycle flows.

All three flows (welcome / reset / verify) render plain-text templates.
When we later migrate to HTML+plain multi-part mails, this is the only
module that changes — all callers remain on ``send_account_email``.
"""

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.urls import reverse


def _absolute_link(url_name, **kwargs):
    """Compose an absolute URL for an email-link.

    We use ``settings.SITE_URL`` (https://remote.oe5xrx.org in prod,
    http://localhost:8000 in dev), NOT request.build_absolute_uri —
    emails go out from background paths that don't always have a
    request available.
    """
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    path = reverse(url_name, kwargs=kwargs)
    return f"{base}{path}"


def send_account_email(user, kind, context):
    """Dispatch a templated plain-text email to the user.

    kind: "welcome" | "reset" | "verify"
    context: dict for template rendering. Must contain "raw_token". May
             contain "override_to" (verify uses this to send to NEW email
             instead of user.email), "actor" (welcome shows admin user),
             "new_email" / "old_email" (verify).
    """
    raw_token = context["raw_token"]
    if kind == "verify":
        link = _absolute_link("accounts:verify_email", token=raw_token)
    else:
        link = _absolute_link("accounts:set_password", token=raw_token)

    ctx = {
        "user": user,
        "link": link,
        "site_url": getattr(settings, "SITE_URL", ""),
        **context,
    }
    subject = render_to_string(f"accounts/emails/{kind}.subject.txt", ctx).strip()
    body = render_to_string(f"accounts/emails/{kind}.body.txt", ctx)
    to_email = context.get("override_to") or user.email
    send_mail(
        subject=subject,
        message=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[to_email],
        fail_silently=False,
    )
