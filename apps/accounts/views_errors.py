from django.shortcuts import render

from apps.accounts.permissions import AreaDenied, level_label, verb_label


def permission_denied(request, exception=None):
    ctx = {"message": str(exception) if exception and str(exception) else ""}
    if isinstance(exception, AreaDenied):
        ctx.update(
            area_denied=True,
            what=verb_label(exception.area, exception.verb),
            need=level_label(exception.required_level),
            have=level_label(exception.actual_level),
        )
    return render(request, "403.html", ctx, status=403)
