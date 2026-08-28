"""PWA asset views (service worker + manifest) and subscription API.

The service worker and manifest are served through Django rather than
static files because WhiteNoise's ManifestStaticFilesStorage hashes
filenames — a service worker needs a stable URL and root scope.
"""

import json

from django.contrib.auth.decorators import login_not_required, login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET, require_POST

from .models import PushSubscription


@login_not_required
@require_GET
def service_worker(request):
    response = render(request, "webpush/sw.js", content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    response["Cache-Control"] = "no-cache"
    return response


@login_not_required
@require_GET
def manifest(request):
    data = {
        "name": "OE5XRX Station Manager",
        "short_name": "OE5XRX",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#120A04",
        "theme_color": "#FF8A3D",
        "icons": [
            {
                "src": static("webpush/icon-192.png"),
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any maskable",
            },
            {
                "src": static("webpush/icon-512.png"),
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any maskable",
            },
        ],
    }
    return JsonResponse(data, content_type="application/manifest+json")


@login_required
@require_POST
def subscribe(request):
    try:
        body = json.loads(request.body)
        endpoint = body["endpoint"]
        keys = body["keys"]
        p256dh, auth = keys["p256dh"], keys["auth"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False, "error": "invalid payload"}, status=400)

    label = request.META.get("HTTP_USER_AGENT", "")[:120]

    # The endpoint is a secret, per-browser capability URL. Creating it, or the
    # same user re-subscribing, is fine — but do NOT let one account silently
    # take over an endpoint already registered to a different account. That
    # would re-home another user's device (redirecting its pushes) on nothing
    # more than knowledge of the endpoint. Reject it; the owner must remove the
    # device first.
    existing = PushSubscription.objects.filter(endpoint=endpoint).first()
    if existing is not None and existing.user_id != request.user.id:
        return JsonResponse(
            {"ok": False, "error": "endpoint already registered to another account"},
            status=409,
        )

    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={
            "user": request.user,
            "p256dh": p256dh,
            "auth": auth,
            "label": label,
            "failure_count": 0,
        },
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def unsubscribe(request):
    try:
        endpoint = json.loads(request.body)["endpoint"]
    except (ValueError, KeyError, TypeError):
        return JsonResponse({"ok": False, "error": "invalid payload"}, status=400)

    # Scoped to the caller — a user can only remove their own subscription.
    PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})
