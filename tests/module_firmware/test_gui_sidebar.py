import re

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()


@pytest.fixture
def applicant(db):
    return User.objects.create_user(username="a", password="x", membership_level="applicant")


def _sidebar_item(html, url):
    """Return the opening <a ...> tag of the sidebar nav-item linking to url."""
    for tag in re.findall(r"<a [^>]*class=\"nav-item[^>]*>", html):
        if f'href="{url}"' in tag:
            return tag
    return None


def test_sidebar_has_module_firmware_section(client, applicant):
    client.force_login(applicant)
    html = client.get(reverse("dashboard:index")).content.decode()
    assert "Module Firmware" in html
    for name in ("module_list", "release_list", "target_list", "convergence", "type_list"):
        assert _sidebar_item(html, reverse(f"module_firmware:{name}")), name


def test_modules_item_moved_out_of_operations(client, applicant):
    client.force_login(applicant)
    html = client.get(reverse("dashboard:index")).content.decode()
    ops = html.index("Operations")
    section = html.index("Module Firmware")
    assert ops < section
    assert html[ops:section].count(reverse("module_firmware:module_list")) == 0


@pytest.mark.parametrize(
    ("page", "active"),
    [
        ("module_firmware:target_list", "target_list"),
        ("module_firmware:type_list", "type_list"),
        ("module_firmware:release_list", "release_list"),
        ("module_firmware:convergence", "convergence"),
        ("module_firmware:module_list", "module_list"),
    ],
)
def test_only_matching_item_is_active(client, applicant, page, active):
    client.force_login(applicant)
    html = client.get(reverse(page)).content.decode()
    names = ("module_list", "release_list", "target_list", "convergence", "type_list")
    for name in names:
        tag = _sidebar_item(html, reverse(f"module_firmware:{name}"))
        if name == active:
            assert 'aria-current="page"' in tag and " active" in tag, name
        else:
            assert 'aria-current="page"' not in tag, name
