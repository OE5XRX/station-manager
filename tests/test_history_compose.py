"""Deploy: the history maintainer runs as its own compose service (dev + prod).

Shape must match ``alert-monitor`` in the same file (image/build, env, depends_on, restart,
volumes); only the command differs.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMMAND = "python manage.py history_maintain --loop --interval 600"


@pytest.mark.parametrize("path", ["docker-compose.yml", "deploy/docker-compose.prod.yml"])
def test_history_maintainer_service_mirrors_alert_monitor(path):
    services = yaml.safe_load((ROOT / path).read_text())["services"]
    maintainer = services["history-maintainer"]
    assert maintainer["command"] == COMMAND
    reference = dict(services["alert-monitor"])
    reference.pop("command")
    assert {k: v for k, v in maintainer.items() if k != "command"} == reference
