from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path
from oauth2_provider import urls as dot_urls

from apps.sso.views import AppGrantAuthorizationView

# Only DOT's protocol endpoints are mounted. DOT's management_urlpatterns
# (applications/, authorized_tokens/) and dcr_urlpatterns are deliberately
# left out: client management lives in apps.sso (sso.manage). The RFC 8628
# device grant is dropped from base_urlpatterns too: it bypasses the per-app
# SSO policy (AppGrantAuthorizationView) and no RP uses it.
_DOT_DEVICE_FLOW = frozenset(
    {"device-authorization", "device", "device-confirm", "device-grant-status"}
)
DOT_PROTOCOL_URLPATTERNS = (
    dot_urls.metadata_urlpatterns
    + [p for p in dot_urls.base_urlpatterns if p.name not in _DOT_DEVICE_FLOW]
    + dot_urls.oidc_urlpatterns
)

urlpatterns = [
    path("", include("apps.webpush.urls")),
    path("api/", include("apps.api.urls")),
    path(
        "api/v1/module-firmware/",
        include(("apps.module_firmware.api_urls", "module_firmware_api")),
    ),
    path("i18n/", include("django.conf.urls.i18n")),
    # Override DOT's authorize endpoint with our AppGrant-gated version
    # BEFORE the DOT include — Django picks the first match.
    path(
        "sso/authorize/",
        AppGrantAuthorizationView.as_view(),
        name="sso-authorize",
    ),
    # OIDC endpoints — kept out of i18n_patterns so well-known URLs
    # don't carry a locale prefix that breaks RP discovery.
    path(
        "sso/", include((DOT_PROTOCOL_URLPATTERNS, "oauth2_provider"), namespace="oauth2_provider")
    ),
]

urlpatterns += i18n_patterns(
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("sso-admin/", include("apps.sso.urls")),
    path("stations/", include("apps.stations.urls")),
    path("stations/", include("apps.control.urls")),
    path("deployments/", include("apps.deployments.urls")),
    path("tunnel/", include("apps.tunnel.urls")),
    path("audit/", include("apps.audit.urls")),
    path("monitoring/", include("apps.monitoring.urls")),
    path("images/", include("apps.images.urls")),
    path("modules/", include("apps.module_firmware.urls")),
    path("provisioning/", include("apps.provisioning.urls")),
    path("rollouts/", include("apps.rollouts.urls")),
    path("", include("apps.dashboard.urls")),
)

if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
    ] + urlpatterns

handler403 = "apps.accounts.views_errors.permission_denied"
