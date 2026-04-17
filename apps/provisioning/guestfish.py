from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# Partition index where `data` lives per the wks layouts in
# meta-oe5xrx-remotestation/wic/:
#   x86-64: 4 partitions (EFI, rootfs-A, rootfs-B, data) -> /dev/sda4
#   RPi:    8 partitions, data is last                   -> /dev/sda8
DATA_PARTITION = {
    "qemux86-64": "/dev/sda4",
    "raspberrypi4-64": "/dev/sda8",
}


class GuestfishError(RuntimeError):
    pass


def inject_provisioning_files(
    *,
    wic_path: Path,
    partition_device: str,
    config_yaml: str,
    private_key_pem: bytes,
) -> None:
    """Mount the data partition of `wic_path` and write the provisioning bundle."""
    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.yml"
        key_path = Path(tmp) / "device_key.pem"
        config_path.write_text(config_yaml)
        key_path.write_bytes(private_key_pem)

        script = "\n".join(
            [
                "run",
                f"mount {partition_device} /",
                "mkdir-p /etc-overlay/station-agent",
                f"upload {config_path} /etc-overlay/station-agent/config.yml",
                f"upload {key_path} /etc-overlay/station-agent/device_key.pem",
                "chmod 0600 /etc-overlay/station-agent/device_key.pem",
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


def data_partition_for(machine: str) -> str:
    try:
        return DATA_PARTITION[machine]
    except KeyError:
        raise ValueError(f"unsupported machine: {machine}") from None
