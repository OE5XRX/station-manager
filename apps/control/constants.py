# apps/control/constants.py
from django.conf import settings

# §7 envelope version. The server-synthesized connect-time inventory snapshot
# carries the same "v" as the agent's relayed frames so D5 sees one shape.
PROTOCOL_VERSION = 1

T_IDLE_SECONDS = getattr(settings, "CONTROL_T_IDLE_SECONDS", 300)
RECONNECT_GRACE_SECONDS = getattr(settings, "CONTROL_RECONNECT_GRACE_SECONDS", 12)
COMMAND_TIMEOUT_SECONDS = getattr(settings, "CONTROL_COMMAND_TIMEOUT_SECONDS", 10)
LOCK_SWEEP_INTERVAL_SECONDS = getattr(settings, "CONTROL_LOCK_SWEEP_INTERVAL_SECONDS", 5)

# NOTE: no CONTROL_MAX_VIEWERS_PER_STATION — viewers are cheap (WS + broadcasts);
# the TX-lock + PTT dead-man are the real safeguards. Conscious §10 deviation.
