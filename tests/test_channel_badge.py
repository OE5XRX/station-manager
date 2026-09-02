from django.template.loader import render_to_string


def test_channel_badge_dev_is_prominent():
    html = render_to_string("images/_channel_badge.html", {"channel": "dev"})
    assert "DEV" in html
    assert "pill-accent" in html


def test_channel_badge_release_is_muted():
    html = render_to_string("images/_channel_badge.html", {"channel": "release"})
    assert "RELEASE" in html
    assert "pill-muted" in html


def test_channel_badge_empty_renders_nothing():
    html = render_to_string("images/_channel_badge.html", {"channel": ""})
    assert html.strip() == ""
