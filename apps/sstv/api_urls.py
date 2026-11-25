from django.urls import path

from . import api_views

app_name = "sstv_api"
urlpatterns = [
    path("tx/<uuid:tx_id>/audio/", api_views.TxAudioView.as_view(), name="tx_audio"),
]
