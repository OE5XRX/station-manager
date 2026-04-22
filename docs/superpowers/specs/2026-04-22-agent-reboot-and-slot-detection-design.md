# Agent-side OTA hardening: real reboot, runtime slot detection, trial-flag guard

Date: 2026-04-22

## Problem

The 2026-04-22 end-to-end OTA test on the qemux86-64 station exposed
three gaps in `station_agent`'s install/commit flow:

1. **No real reboot.** After `set_upgrade_pending(slot_b)`, the agent
   logs "in production, device would reboot now" and calls
   `_verify_and_commit` inline. On real hardware this means the
   bootloader env flags `boot_part=b upgrade_available=1 bootcount=0`
   while the kernel continues running from slot_a — the machine
   never actually tries the new slot.

2. **`get_active_slot()` reads the bootloader env, not the running
   rootfs.** The env's `boot_part` is a "next-boot hint" that
   `set_upgrade_pending` mutates *before* reboot. Without a real
   reboot (or if one crashes / is aborted mid-flight), the
   subsequent `get_inactive_slot()` call returns the slot the
   kernel is actually running from — and `apply_update` writes into
   it. Observed: two consecutive OTA runs on the test station
   overwrote the live slot on the second pass.

3. **Post-reboot verify trusts `/etc/os-release` alone.** Correct
   for `v1 → v2` upgrades: a bootloader-rollback lands the kernel
   on slot_a with v1, version check catches the mismatch. Fails
   for "same-version redeploy" (e.g. `v1 → v1` or a re-deploy of
   the already-running tag): both slots report the same version
   string, so a silent rollback is indistinguishable from a
   successful trial.

Plus one cosmetic follow-up from PR #28: the agent's local
`dest_path` template still says `.wic.bz2` even though the
server now ships a rootfs artifact. Confusing for anyone reading
`/tmp/station-agent/` contents.

## Decisions

| Decision | Choice | Reasoning |
| --- | --- | --- |
| Reboot mechanism | `subprocess.run(["systemctl", "reboot"])` | Standard on every Yocto-produced image. Inline-verify-after-reboot codepath is removed entirely; the existing post-reboot-recovery path in `agent.py` is the single verify entrypoint. Prod and test share one codepath; tests mock `subprocess.run` at the boundary. |
| Active-slot detection | Runtime-derived, bootloader-agnostic | `/proc/cmdline` primary probe (`root=PARTLABEL=root_[ab]`), `os.stat("/")` vs partlabel symlinks as fallback. Fail closed with `RuntimeError` if neither resolves — guessing a default risks overwriting the running rootfs. Grub and u-boot both emit `root=PARTLABEL=root_${boot_part}` in the kernel cmdline, so one regex handles both. |
| Rollback detection in verify | Add `upgrade_available == "1"` guard alongside the version check | `boot.cmd` (u-boot) and `oe5xrx-grub.cfg` (grub) both set `upgrade_available=0` when they roll back after `bootcount > bootlimit`. Reading the post-reboot env via the existing `get_env()` helper catches same-version rollbacks that the `/etc/os-release` comparison can't. |
| Filename | `firmware-{version}.rootfs.bz2` | Matches what's actually being downloaded post-PR#28. Old `firmware-*.wic.bz2` partials in `/tmp/station-agent/` from previous agent versions are orphaned — proactively `glob` + remove on first download pass. |
| `get_active_slot` parameter | Keep `bootloader` arg for API stability, ignore it | The runtime detection is bootloader-agnostic; dropping the arg would ripple through every caller for no behavioural gain. |

## Architecture

Four agent-side fixes in one PR, all in `station_agent/`. No
server-side changes. Requires one linux-image rebuild to ship the
updated agent; no change to the Yocto recipe other than the new
station-manager SRCREV.

```
station_agent/
    bootloader.py   # get_active_slot rewritten (runtime-based)
    agent.py        # _handle_ota reboots for real; inline verify-and-commit removed
                    # dest_path filename updated; old .wic.bz2 partials purged

tests/
    test_ota_install.py or new test_agent_reboot.py
                    # new tests for the reboot path, runtime slot detection,
                    # upgrade_available rollback guard
```

## `bootloader.get_active_slot` — runtime-derived

```python
_CMDLINE_PATTERN = re.compile(r"root=PARTLABEL=root_([ab])\b")


def _slot_from_cmdline() -> str | None:
    """Primary probe: parse the A/B slot out of /proc/cmdline. Works
    for both GRUB (oe5xrx-grub.cfg) and U-Boot (boot.cmd) because
    the wic recipe uses PARTLABEL and both boot scripts emit the
    same ``root=PARTLABEL=root_${boot_part}`` form."""
    try:
        with open("/proc/cmdline") as f:
            cmdline = f.read()
    except OSError:
        return None
    m = _CMDLINE_PATTERN.search(cmdline)
    return m.group(1) if m else None


def _slot_from_root_mount() -> str | None:
    """Fallback: compare the device backing / against the partlabel
    symlinks. Handles cmdline-editing edge cases (initramfs that
    doesn't forward root=, kernel cmdline stripped by a service)."""
    try:
        root_dev = os.stat("/").st_dev
    except OSError:
        return None
    for slot in ("a", "b"):
        try:
            part = os.stat(f"/dev/disk/by-partlabel/root_{slot}")
        except OSError:
            continue
        if part.st_rdev == root_dev:
            return slot
    return None


def get_active_slot(bootloader: str = "") -> str:
    """Return the currently-running A/B slot.

    Derived from the running rootfs, NOT the bootloader env. The
    env's ``boot_part`` is a "next-boot hint" that ``set_upgrade_pending``
    mutates before reboot; reading it would misidentify the active slot
    in every "set_upgrade_pending-but-not-yet-rebooted" window and let
    ``install_to_slot`` corrupt the running rootfs.

    The ``bootloader`` parameter is kept for API compatibility but
    ignored — detection is bootloader-agnostic.

    Raises RuntimeError if neither probe resolves. Fail closed rather
    than guess — the alternative is a potentially bricked station.
    """
    for probe in (_slot_from_cmdline, _slot_from_root_mount):
        slot = probe()
        if slot in ("a", "b"):
            return slot
    raise RuntimeError(
        "Cannot determine active A/B slot from /proc/cmdline or / mount. "
        "Refusing to guess — next install_to_slot could corrupt the running rootfs."
    )
```

Existing `get_inactive_slot(bootloader)` is unchanged — it already
delegates to `get_active_slot`. `apply_update()` currently logs and
returns `False` when the inactive device doesn't exist; on
`RuntimeError` from `get_active_slot` it now logs and returns
`False` too (so the agent reports FAILED instead of crashing the
worker loop).

## `agent._handle_ota` — real reboot, remove inline verify

Current code (agent.py:144-151):

```python
# Report rebooting (in production, the device would reboot here)
report_status(config, http_client, result_pk, "rebooting")
logger.info("OTA update applied. In production, device would reboot now.")

# Since we are not actually rebooting, run verification immediately.
# In production, this would happen on the next boot.
self._verify_and_commit(config, http_client, result_pk, version)
```

After the change:

```python
# Report rebooting before invoking the reboot syscall so the server
# knows the station is going down intentionally. Best-effort —
# a transient failure here must not block the reboot itself.
report_status(config, http_client, result_pk, "rebooting")

# Real reboot. After this call the agent process is killed by systemd;
# we never reach the line after it. _verify_and_commit runs on the next
# boot via the existing post-reboot-recovery path in check_for_update
# (deployment_result_status in {"rebooting", "verifying"}).
try:
    subprocess.run(["systemctl", "reboot"], check=True)
except (OSError, subprocess.CalledProcessError) as exc:
    # OSError covers FileNotFoundError (no systemctl binary),
    # PermissionError (wrong uid), and rarer low-level syscall
    # failures. CalledProcessError catches non-zero exit codes
    # from systemctl itself (unit refused, inhibitor blocking,
    # etc.). All three mean: slot_b is written and armed but
    # the switch didn't happen. Tell the server so the operator
    # sees FAILED instead of a station stuck forever in
    # "rebooting".
    logger.error("Reboot failed: %s", exc)
    report_status(
        config, http_client, result_pk, "failed",
        error_message=f"Reboot call failed: {exc}",
    )
    return

# Unreachable in production; kept as a safety net in case systemctl
# returns without rebooting for reasons we don't understand.
logger.error("systemctl reboot returned without rebooting — reporting failed")
report_status(
    config, http_client, result_pk, "failed",
    error_message="systemctl reboot returned without rebooting",
)
```

The `_verify_and_commit` method is unchanged in its structure and
stays a method on the agent class — it's still the implementation
called from the post-reboot-recovery path.

## `_verify_and_commit` — trial-flag guard

Current verify check sequence: read `OE5XRX_RELEASE` → compare to
target tag → health checks → commit.

Add one guard between the version check and the health checks:

```python
bl = get_bootloader(config)
upgrade_available = get_env(bl, "upgrade_available")
if upgrade_available != "1":
    report_status(
        config, http_client, result_pk, "rolled_back",
        error_message=(
            f"Bootloader upgrade_available={upgrade_available!r} "
            "(expected '1') — trial boot was rolled back or env read "
            "failed; refusing to commit."
        ),
    )
    logger.warning(
        "Refusing to commit deployment %s: upgrade_available=%r",
        result_pk, upgrade_available,
    )
    return
```

Catches the "same-version redeploy, silent rollback" blind spot that
the `/etc/os-release` comparison can't see, plus any scenario where
the env read itself is unreliable (fail closed on `None`).

## `agent._handle_ota` — filename cosmetic + partial cleanup

Current:
```python
dest_path = os.path.join(config.download_dir, f"firmware-{safe_version}.wic.bz2")
```

Change to `.rootfs.bz2`. Before any download work runs, sweep any
legacy `.wic.bz2` orphans so they don't pile up:

```python
for stale in Path(config.download_dir).glob("firmware-*.wic.bz2"):
    try:
        stale.unlink()
        logger.info("Removed legacy partial: %s", stale)
    except OSError as exc:
        logger.warning("Could not remove legacy partial %s: %s", stale, exc)
```

One-time migration on first run of the new agent. Subsequent runs
find no `*.wic.bz2` files and the glob is a no-op.

## Error handling

| Scenario | Today | After |
| --- | --- | --- |
| `apply_update` on a station where `get_active_slot` can't resolve | Would read bootloader env → potentially return wrong slot → overwrite live rootfs. | `RuntimeError` from `get_active_slot` → `apply_update` returns `False` → agent reports `failed`. |
| Reboot call fails (no systemctl, permission denied) | N/A — no real reboot. | Report `failed` with the exception message; operator sees the station stuck at "installing-but-not-rebooting" and investigates. |
| Reboot call returns without rebooting | N/A. | Same: report `failed`. Safety net for kernels / systemd versions we haven't tested against. |
| Bootloader rolled back (same version in both slots) | Version check passes, commit succeeds — undetected. | `upgrade_available=0` guard triggers `rolled_back`, server records it, station stays in a known-safe state. |
| Bootloader env unreadable during verify | Version check passes, commit succeeds — state drift unnoticed. | `upgrade_available=None` → `rolled_back` with "env read failed" message. Fail closed. |

## Tests

All agent-side unit tests. No Django / server-state required.

### `tests/test_bootloader.py` (new)

Currently no tests exist for the `station_agent.bootloader` module.
Adding a module-level test file for the runtime slot detection (and
future bootloader tests).

- `test_slot_from_cmdline_parses_grub_and_uboot_shapes`: Feed in the
  exact cmdline strings that `oe5xrx-grub.cfg:39` and `boot.cmd:57`
  produce. Both should yield the right slot.
- `test_slot_from_cmdline_returns_none_without_partlabel`: A
  cmdline without `root=PARTLABEL=` (e.g. `root=/dev/sda2`) returns
  `None` so the fallback runs.
- `test_slot_from_root_mount_matches_partlabel_symlink`: Monkeypatch
  `os.stat` to return matching `st_dev` / `st_rdev` for a specific
  slot's partlabel symlink; assert that slot is returned.
- `test_get_active_slot_prefers_cmdline_over_mount`: If both probes
  would succeed with different answers, cmdline wins (i.e. the
  fallback is only consulted when primary returns `None`).
- `test_get_active_slot_raises_when_both_probes_fail`: Monkeypatch
  both `/proc/cmdline` read and `os.stat` to fail; assert
  `RuntimeError` with "refusing to guess" in the message.

### `tests/test_agent_reboot.py` (new)

- `test_handle_ota_issues_real_reboot_after_install`: Mock
  `subprocess.run` and the HTTP client; drive `_handle_ota` through
  a successful install; assert `subprocess.run` was called with
  `["systemctl", "reboot"]` and `report_status("rebooting")` was
  called first.
- `test_handle_ota_reports_failed_when_reboot_call_errors`:
  `subprocess.run` raises `FileNotFoundError`; assert
  `report_status("failed", ...)` with the error message in the body.
- `test_handle_ota_does_not_call_verify_inline`: Assert that
  `_verify_and_commit` is NOT invoked in the same `_handle_ota`
  call that issues the reboot. (Belt-and-braces for the
  "inline-verify code is gone" invariant.)

### `tests/test_ota_install.py` (extended)

- `test_verify_and_commit_rolls_back_when_upgrade_available_zero`:
  Mock `get_env` to return `"0"` for `upgrade_available`; assert
  `report_status("rolled_back", ...)` with the "trial boot was
  rolled back" message; assert `commit_boot` NOT called.
- `test_verify_and_commit_rolls_back_when_upgrade_available_missing`:
  Same with `None` from `get_env`.

### `tests/test_agent_reboot.py` (one more test)

- `test_handle_ota_removes_legacy_wic_partials_before_download`:
  Seed the temp `download_dir` with a stale `firmware-XXXX.wic.bz2`;
  run `_handle_ota`; assert the stale file is gone before the
  download step starts.

## Rollout

1. Merge this PR to `main`.
2. Bump `SRCREV` in the linux-image recipe (`scripts/pin-station-agent.sh`),
   merge that PR.
3. Cut a new linux-image release tag.
4. Reflash the Proxmox test station with the new image (we're still
   in the pre-OTA-chicken-and-egg phase for this agent, because the
   reboot fix itself lives in the new agent).
5. Trigger a test OTA upgrade to the same tag (`2026.MM.DD-HH`).
   Expected sequence:
   - `DOWNLOADING`: rootfs arrives from the re-imported ImageRelease.
   - `INSTALLING`: writes to the correct inactive slot (runtime-
     detected, not env-derived).
   - `REBOOTING`: agent reports, then `systemctl reboot` fires.
   - Station goes down, grub trial-boots slot_b.
   - `VERIFYING`: post-reboot agent confirms `OE5XRX_RELEASE` tag
     AND `upgrade_available=1`.
   - `SUCCESS`: `commit_boot` sets `upgrade_available=0 bootcount=0`
     and POSTs `/api/v1/deployments/commit/`.
6. For extra safety, run the same test with v1 → v2 to exercise a
   genuine version change through the whole flow.

## Out of scope

- **Explicit partition-identity audit log during commit.** The
  `upgrade_available` guard already covers the correctness-critical
  "silent rollback" case; a separate audit log entry comparing
  `get_active_slot()` against the deployment's intended slot would
  be nice-to-have for after-the-fact forensics but doesn't close a
  correctness hole. If we ever need it, it's a clean additive change.
- **Dropping the `bootloader` argument from `get_active_slot`.** The
  detection is now bootloader-agnostic, so the arg is ignored. We
  leave it in for API stability — removing it would force every
  caller (and every test) to change. Worth revisiting only as part
  of a broader bootloader-API cleanup.
- **Rebooting via D-Bus / `systemd.manager.Reboot()`.** A subprocess
  call to `systemctl reboot` is simpler and matches what scripts
  across the image already do.
- **Test covering the actual reboot landing into slot_b.** End-to-end
  reboot test needs a real (or realistic-QEMU) environment and is
  out of scope for pytest. The post-reboot-recovery path in
  `_handle_ota` is already covered by the existing check-endpoint
  tests on the server side; the new tests above pin the pre-reboot
  side.
