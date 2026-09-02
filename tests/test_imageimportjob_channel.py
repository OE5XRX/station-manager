import pytest

from apps.images.models import ImageImportJob


@pytest.mark.django_db
def test_import_job_channel_default():
    j = ImageImportJob.objects.create(tag="v1", machine="qemux86-64")
    assert j.channel == "release"


@pytest.mark.django_db
def test_import_job_channel_set():
    j = ImageImportJob.objects.create(tag="v1", machine="qemux86-64", channel="dev")
    assert j.channel == "dev"
