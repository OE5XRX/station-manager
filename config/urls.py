from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path

from apps.sso.views import AppGrantAuthorizationView

urlpatterns = [
    path("api/", include("apps.api.urls")),
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
    path("sso/", include("oauth2_provider.urls", namespace="oauth2_provider")),
]

urlpatterns += i18n_patterns(
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls")),
    path("sso-admin/", include("apps.sso.urls")),
    path("stations/", include("apps.stations.urls")),
    path("deployments/", include("apps.deployments.urls")),
    path("tunnel/", include("apps.tunnel.urls")),
    path("audit/", include("apps.audit.urls")),
    path("monitoring/", include("apps.monitoring.urls")),
    path("images/", include("apps.images.urls")),
    path("provisioning/", include("apps.provisioning.urls")),
    path("rollouts/", include("apps.rollouts.urls")),
    path("", include("apps.dashboard.urls")),
)

if settings.DEBUG:
    import debug_toolbar

    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
    ] + urlpatterns
