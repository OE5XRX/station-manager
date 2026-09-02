import pytest


@pytest.mark.django_db
def test_current_image_variant_defaults_empty(station):
    assert station.current_image_variant == ""
