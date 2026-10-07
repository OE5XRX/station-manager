"""Template helper: is the viewer allowed to change a capability? (cosmetic only —
the server-side gate in the control consumer is the real enforcement)."""

from django import template

from apps.control import capability_policy

register = template.Library()


@register.simple_tag(takes_context=True)
def cap_access(context, cap, module):
    name = cap.get("name") if isinstance(cap, dict) else None
    policy = capability_policy.policy_for(name, getattr(module, "type", None))
    if "viewer_role" in context:
        role = context["viewer_role"]
    else:
        request = context.get("request")
        station = context.get("station")
        role = (
            capability_policy.viewer_role(getattr(request, "user", None), station)
            if station is not None
            else None
        )
    # Fail closed: any unresolved role renders read-only.
    return {
        "writable": capability_policy.role_allows(role, policy.write_role),
        "write_role": policy.write_role,
        "write_role_label": capability_policy.ROLE_LABELS.get(
            policy.write_role, policy.write_role
        ),
    }
