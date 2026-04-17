from django.urls import path

from . import views

app_name = "audit"

urlpatterns = [
    path("", views.AuditLogListView.as_view(), name="audit_list"),
    path("export/", views.AuditLogExportView.as_view(), name="audit_export"),
]
