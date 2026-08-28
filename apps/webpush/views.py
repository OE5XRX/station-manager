"""PWA asset views (service worker + manifest) and subscription API.

The service worker and manifest are served through Django rather than
static files because WhiteNoise's ManifestStaticFilesStorage hashes
filenames — a service worker needs a stable URL and root scope.
"""

from django.contrib.auth.decorators import login_not_required
from django.http import JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.http import require_GET


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
