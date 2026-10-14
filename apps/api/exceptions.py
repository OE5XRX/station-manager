from rest_framework.response import Response
from rest_framework.views import exception_handler

from apps.accounts.permissions import AreaDenied


def area_exception_handler(exc, context):
    if isinstance(exc, AreaDenied):
        return Response(exc.as_api_body(), status=403)
    return exception_handler(exc, context)
