import pytest
from django.http import HttpResponse
from django.template import Context, Template
from django.urls import path
from django.views import View

from apps.accounts.models import AccountAuditLog, User
from apps.accounts.permissions import AreaPermissionMixin, area_permission, permission_exempt


class ImagesView(AreaPermissionMixin, View):
    area = "images"

    def get(self, request):
        return HttpResponse("list")

    def post(self, request):
        return HttpResponse("changed")


class TokenView(AreaPermissionMixin, View):
    area = "api_tokens"
    verb = "use"

    def get(self, request):
        return HttpResponse("ok")

    def post(self, request):
        return HttpResponse("ok")


@area_permission("audit", "view")
def audit_fn(request):
    return HttpResponse("audit")


@permission_exempt("test: public")
def public_fn(request):
    return HttpResponse("pub")


_TEST_PATTERNS = [
    path("t/images/", ImagesView.as_view()),
    path("t/tokens/", TokenView.as_view()),
    path("t/audit/", audit_fn),
    path("t/pub/", public_fn),
]


def __getattr__(name):
    # Resolved lazily at request time: 403.html extends base.html, which reverses the
    # project's named URLs, so the project urlconf is appended. Importing config.urls at
    # collection time would pull in debug_toolbar before settings are fully ready.
    if name == "urlpatterns":
        from config.urls import urlpatterns as project_urlpatterns

        return _TEST_PATTERNS + list(project_urlpatterns)
    raise AttributeError(name)


handler403 = "apps.accounts.views_errors.permission_denied"


def _u(level):
    return User.objects.create_user(username=f"w-{level}", password="x", membership_level=level)


pytestmark = [pytest.mark.django_db, pytest.mark.urls("tests.test_permission_web_binding")]


def test_safe_method_needs_view(client):
    client.force_login(_u("applicant"))
    assert client.get("/t/images/").status_code == 200


def test_unsafe_method_needs_manage_and_renders_403_page(client):
    client.force_login(_u("staff"))
    r = client.post("/t/images/")
    assert r.status_code == 403
    body = r.content.decode()
    assert "Keine Berechtigung" in body
    assert "Vereins-Admin" in body and "Vereins-Staff" in body
    assert AccountAuditLog.objects.filter(event_type="permission_denied").count() == 1


def test_explicit_verb_applies_to_all_methods(client):
    client.force_login(_u("member"))
    assert client.post("/t/tokens/").status_code == 200
    client.force_login(_u("applicant"))
    assert client.get("/t/tokens/").status_code == 403


def test_function_decorator(client):
    client.force_login(_u("staff"))
    assert client.get("/t/audit/").status_code == 403
    client.force_login(_u("admin"))
    assert client.get("/t/audit/").status_code == 200


def test_anonymous_redirects_to_login_not_logged(client):
    r = client.get("/t/images/")
    assert r.status_code == 302 and "/login" in r["Location"]
    assert not AccountAuditLog.objects.filter(event_type="permission_denied").exists()


def test_exempt_marker():
    assert public_fn.permission_exempt == "test: public"


def test_generic_permission_denied_uses_same_page(client, rf):
    from django.core.exceptions import PermissionDenied

    from apps.accounts.views_errors import permission_denied

    req = rf.get("/x/")
    req.user = _u("member")
    resp = permission_denied(req, PermissionDenied("Du kannst dich nicht selbst herabstufen."))
    assert resp.status_code == 403
    assert "Du kannst dich nicht selbst herabstufen." in resp.content.decode()


def test_can_context_processor(rf):
    from apps.accounts.context_processors import permissions

    req = rf.get("/")
    req.user = _u("staff")
    ctx = permissions(req)
    tpl = Template("{% if can.alerts.manage %}A{% endif %}{% if can.images.manage %}B{% endif %}")
    assert tpl.render(Context(ctx)) == "A"
