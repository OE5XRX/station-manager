from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.urls import include, path
from django.utils import timezone
from django.views.decorators.cache import cache_control
from django.views.decorators.http import last_modified
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

# The catalog only changes with a deploy (compiled .mo files), i.e. with a
# new process. Browsers get a short max-age plus Last-Modified = process
# start, so they revalidate cheaply (304) and pick up new translations within
# minutes of a deploy. No server-side cache_page: the response varies on
# Cookie (session) and the key includes the query string, so it would neither
# hit nor be safe for the shared default cache (anonymous ?x=N spam evicts
# throttle counters); building the catalog is cheap.
_CATALOG_LAST_MODIFIED = timezone.now().replace(microsecond=0)
_CATALOG_BROWSER_MAX_AGE = 300


def _catalog_last_modified(request, **kwargs):
    return _CATALOG_LAST_MODIFIED


javascript_catalog = login_not_required(
    last_modified(_catalog_last_modified)(
        cache_control(max_age=_CATALOG_BROWSER_MAX_AGE)(JavaScriptCatalog.as_view())
    )
)

urlpatterns = [
    path("", include("apps.webpush.urls")),
    # Agent download of SSTV TX audio (DeviceKey), before the generic API include.
    path("api/v1/stations/sstv/", include("apps.sstv.api_urls")),
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
    path("jsi18n/", javascript_catalog, name="javascript-catalog"),
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("sso-admin/", include("apps.sso.urls")),
    path("stations/", include("apps.stations.urls")),
    path("stations/", include("apps.control.urls")),
    path("stations/", include("apps.sstv.urls")),
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
