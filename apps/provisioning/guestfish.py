from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# The persistent partition carries the ext4 filesystem label "data" in every
# wks layout (linux-image/meta-oe5xrx-remotestation/wic/*.wks.in), independent
# of its position in the GPT table:
#   x86-64: 4 partitions, data is #4
#   RPi:    6 partitions, data is #6
# We resolve it by label instead of a hardcoded /dev/sdaN index. A hardcoded
# index silently rots when a layout adds or removes partitions — that is
# exactly how RPi ended up pointing at a non-existent /dev/sda8 after the
# boot_a/boot_b partitions were dropped.
DATA_LABEL = "data"


class GuestfishError(RuntimeError):
    pass


def _guestfish(wic_path: Path, script: str, *, rw: bool) -> str:
    """Run a guestfish script against `wic_path`, returning its stdout.
    Raises GuestfishError on a non-zero exit (fail loud, never silent)."""
    result = subprocess.run(
        ["guestfish", "--rw" if rw else "--ro", "-a", str(wic_path)],
        input=script.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        raise GuestfishError(
            f"guestfish failed ({result.returncode}): {result.stderr.decode('utf-8', 'replace')}"
        )
    return result.stdout.decode("utf-8", "replace")


def _resolve_data_device(wic_path: Path) -> str:
    """Return the single partition device whose filesystem label is
    DATA_LABEL. Raise GuestfishError unless exactly one partition carries the
    label: zero means a broken image, more than one means layout drift — both
    must fail loud rather than silently mount the wrong partition (which would
    be worse than the crash this label-based lookup replaced)."""
    listing = _guestfish(wic_path, "run\nlist-filesystems", rw=False)
    # "list-filesystems" prints "<device>: <fstype>". Raw partitions without a
    # filesystem (e.g. the RPi u-boot env partitions) report "unknown" and must
    # be skipped — vfs-label errors on them.
    devices = [
        line.split(":", 1)[0].strip()
        for line in listing.splitlines()
        if ":" in line and line.split(":", 1)[1].strip() != "unknown"
    ]
    if not devices:
        raise GuestfishError(f"no mountable filesystems in {wic_path}")

    script = "run\n" + "\n".join(f"vfs-label {device}" for device in devices)
    labels = _guestfish(wic_path, script, rw=False).splitlines()
    matches = [device for device, label in zip(devices, labels) if label.strip() == DATA_LABEL]
    if len(matches) != 1:
        raise GuestfishError(
            f"expected exactly one partition labeled {DATA_LABEL!r} in "
            f"{wic_path}, found {len(matches)}: {matches or 'none'}"
        )
    return matches[0]


def inject_provisioning_files(
    *,
    wic_path: Path,
    config_yaml: str,
    private_key_pem: bytes,
) -> None:
    """Mount the data partition of `wic_path` (resolved by filesystem label,
    layout-agnostic across all machines) and write the provisioning bundle."""
    device = _resolve_data_device(wic_path)

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "config.yml"
        key_path = Path(tmp) / "device_key.pem"
        config_path.write_text(config_yaml)
        # Never let the private key be world-readable, even briefly, on the
        # host: create it 0600 up front. (The in-image copy is chmod'd below.)
        with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as fh:
            fh.write(private_key_pem)

        script = "\n".join(
            [
                "run",
                f"mount {device} /",
                "mkdir-p /etc-overlay/stationagent",
                f"upload {config_path} /etc-overlay/stationagent/config.yml",
                f"upload {key_path} /etc-overlay/stationagent/device_key.pem",
                "chmod 0600 /etc-overlay/stationagent/device_key.pem",
                "umount-all",
            ]
        )
        _guestfish(wic_path, script, rw=True)
