from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path
from django.views.decorators.cache import cache_page
from django.views.i18n import JavaScriptCatalog
from oauth2_provider import urls as dot_urls

from apps.accounts.views import set_language
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
    # Our switcher (persists User.language) at Django's own path and name,
    # BEFORE Django's include — first match wins; the include stays so the
    # stock route keeps existing. LoginRequiredMiddleware's allow-list and
    # PrivacyAckMiddleware match this exact path / name.
    path("i18n/setlang/", set_language, name="set_language"),
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
    # JS translation catalog: the language comes from the prefix, so the
    # cache key (URL) is per language. Public (the login page uses it) and
    # exempt from the privacy-notice gate (a script can't follow a redirect).
    path(
        "jsi18n/",
        login_not_required(cache_page(86400)(JavaScriptCatalog.as_view())),
        name="javascript-catalog",
    ),
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
    path("repeaters/", include("apps.repeaters.urls")),
    path("passive/", include("apps.passive.urls")),
    path("", include("apps.dashboard.urls")),
)

if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
    ] + urlpatterns

handler403 = "apps.accounts.views_errors.permission_denied"
