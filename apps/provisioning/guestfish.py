from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# The persistent partition carries the ext4 filesystem label "data" in every
# wks layout (linux-image/meta-oe5xrx-remotestation/wic/*.wks.in), independent
# of its position in the GPT table:
#   x86-64: 4 partitions, data is #4
#   RPi:    6 partitions, data is #6
# We mount it by label via /dev/disk/by-label/ instead of a hardcoded
# /dev/sdaN index. A hardcoded index silently rots when a layout adds or
# removes partitions — that is exactly how RPi ended up pointing at a
# non-existent /dev/sda8 after the boot_a/boot_b partitions were dropped.
DATA_LABEL = "data"
DATA_LABEL_DEVICE = f"/dev/disk/by-label/{DATA_LABEL}"


class GuestfishError(RuntimeError):
    pass


def inject_provisioning_files(
    *,
    wic_path: Path,
    config_yaml: str,
    private_key_pem: bytes,
) -> None:
    """Mount the data partition of `wic_path` (by filesystem label) and write
    the provisioning bundle. Layout-agnostic across all supported machines."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.yml"
        key_path = Path(tmp) / "device_key.pem"
        config_path.write_text(config_yaml)
        key_path.write_bytes(private_key_pem)

        script = "\n".join(
            [
                "run",
                f"mount {DATA_LABEL_DEVICE} /",
                "mkdir-p /etc-overlay/stationagent",
                f"upload {config_path} /etc-overlay/stationagent/config.yml",
                f"upload {key_path} /etc-overlay/stationagent/device_key.pem",
                "chmod 0600 /etc-overlay/stationagent/device_key.pem",
                "umount-all",
            ]
        )
        result = subprocess.run(
            ["guestfish", "--rw", "-a", str(wic_path)],
            input=script.encode(),
            capture_output=True,
        )
        if result.returncode != 0:
            raise GuestfishError(
                f"guestfish failed ({result.returncode}): "
                f"{result.stderr.decode('utf-8', 'replace')}"
            )
