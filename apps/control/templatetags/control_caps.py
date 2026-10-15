"""Template helper: is the viewer allowed to change a capability? (cosmetic only —
the server-side gate in the control consumer is the real enforcement)."""

from django import template

from apps.accounts.permissions import can, level_label, required_level
from apps.control import capability_policy

register = template.Library()


@register.simple_tag(takes_context=True)
def cap_access(context, cap, module):
    name = cap.get("name") if isinstance(cap, dict) else None
    policy = capability_policy.policy_for(name, getattr(module, "type", None))
    request = context.get("request")
    user = getattr(request, "user", None)
    # Fail closed: no request/user renders read-only.
    return {
        "writable": can(user, "control", policy.verb),
        "verb": policy.verb,
        "required_label": level_label(required_level("control", policy.verb)),
    }
