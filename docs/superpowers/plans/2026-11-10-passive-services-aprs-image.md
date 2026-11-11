# Passive Services & APRS — Image + Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Direwolf and the sandboxed `oe5xrx-decoder@.service` template in the station image, then bump the station-agent and add the `aprs-igate` worker to the production compose.

**Architecture:** linux-image gets two recipes:
- `direwolf` (upstream 1.8.1, CMake, ALSA only).
- `oe5xrx-decoder`: a system user `oe5xrx-decoder` in group `pipewire`, the template unit, and the wrapper `oe5xrx-decoder-run`. The wrapper reads the env file the agent writes (`/run/oe5xrx/decoder/<instance>.env`) and runs `pw-record | direwolf` with KISS on `127.0.0.1:<port>`.

The servers repo gets one compose service. The station-manager side is the main plan, `2026-11-10-passive-services-aprs.md`.

**Tech Stack:** Yocto wrynose (kas), bitbake `useradd` + `systemd` classes, bash, pytest static tests (`-m unit`), docker compose.

**Spec:** `docs/superpowers/specs/2026-11-10-passive-services-aprs-design.md` (§5.2, §5.3, §9, §10, §13 "linux-image").

## Global Constraints

- Repos: `OE5XRX/linux-image` (one PR, squash) and `OE5XRX/servers` (one small PR). Work in worktrees, never in the main checkout.
- Direwolf **1.8.1**, the newest stable release (GitHub `wb2osz/direwolf`, tag `1.8.1`, 2025-11-13). It is GPL-2.0, a separate program. Pin `SRCREV` to the tag's commit (`git ls-remote https://github.com/wb2osz/direwolf refs/tags/1.8.1^{}`). No `AUTOREV`.
- Interface contract with the agent (main plan, Tasks 4/5). These names are fixed:
  - Unit `oe5xrx-decoder@<decoder>-slot<N>.service`, e.g. `oe5xrx-decoder@aprs-slot1.service`.
  - Env file `/run/oe5xrx/decoder/<instance>.env`, written by the agent (root).
  - Env keys: `OE5XRX_DECODER`, `OE5XRX_SLOT`, `OE5XRX_RX_NODE` (PipeWire node name, resolved by the agent), `OE5XRX_KISS_PORT` (`8100 + slot`), `OE5XRX_SAMPLE_RATE` (`48000`).
  - The agent runs `systemctl start|stop|is-active` and `systemctl stop "oe5xrx-decoder@*.service"`.
- Direwolf is RX-only: `ADEVICE stdin null`, no `PTT`, no `PBEACON`/`CBEACON`/`IGSERVER`/`DIGIPEAT`, `AGWPORT 0`. Decoded packets go **only** to KISS. stdout is discarded (`StandardOutput=null`).
- Journal protection (spec §2 anti-goal, 64 MB journal): `LogLevelMax=notice`, `LogRateLimitIntervalSec=30s`, `LogRateLimitBurst=20`.
- The rootfs is read-only. Everything runtime-generated lives under `/run` (`RuntimeDirectory=`).
- PipeWire is system-wide as user `pipewire`. Clients join group `pipewire` and use `PIPEWIRE_RUNTIME_DIR=/run/pipewire` (contract in `recipes-multimedia/oe5xrx-audio-system/files/oe5xrx-pipewire.service`).
- Static tests live in `tests/ota-integration/test_*_static.py` with `pytestmark = pytest.mark.unit`. Pattern: `tests/ota-integration/test_ina226_static.py` (`_read`, `_code_lines`).

## Review Focus

1. **KISS port reachable from the network.** Direwolf's `KISSPORT` listens on all interfaces. Expect: nothing outside localhost can connect, enforced by `IPAddressDeny=any` + `IPAddressAllow=localhost` in the unit. This needs `CONFIG_CGROUP_BPF` in both kernels. Pinned by `test_unit_network_sandbox` (Task 2) and the on-device check in Task 3, step 4.
2. **Env file with hostile or garbled values** (newline, shell metachars in the node name). Expect: the wrapper refuses to start (exit 2, one journal line), and nothing is interpolated into a shell. Pinned by `test_wrapper_rejects_bad_env` (Task 2).
3. **pw-record dies while Direwolf keeps running, or the reverse.** Expect: the unit fails as a whole (`set -o pipefail`, `exec` of the pipeline under one bash) and systemd restarts it. Restarts are bounded by `StartLimitBurst`, so a broken node cannot spin forever. Pinned by `test_wrapper_pipeline_failure_propagates` (Task 2).
4. **Decoder user lacking PipeWire access after an image update** (group created in a different order). Expect: `oe5xrx-decoder` is in group `pipewire` in the final `/etc/group`. Pinned by `test_decoder_user_in_pipewire_group` (Task 2, via `USERADD_DEPENDS`) and Task 3, step 2.
5. **Direwolf build silently picking up optional deps** (hamlib/gpsd/udev/avahi) and growing the image. Expect: `DEPENDS` is only `alsa-lib`, and the udev rule and desktop files are removed. Pinned by `test_direwolf_recipe_minimal` (Task 1).

---

### Task 1: Direwolf recipe

**Files:**
- Create: `meta-oe5xrx-remotestation/recipes-multimedia/direwolf/direwolf_1.8.1.bb`
- Modify: `meta-oe5xrx-remotestation/recipes-core/images/oe5xrx-remotestation-image.bb` (append `IMAGE_INSTALL:append = " direwolf oe5xrx-decoder"` next to `oe5xrx-audio-system` at :50, with a one-line comment)
- Test: `tests/ota-integration/test_decoder_static.py`

**Interfaces:**
- Produces the package `direwolf`, containing only `${bindir}/direwolf`. The other upstream tools (`decode_aprs`, `kissutil`, …) go to `${PN}-tools`, which is not installed.

The recipe:
- `LICENSE = "GPL-2.0-only"`, `LIC_FILES_CHKSUM = "file://LICENSE;md5=<from upstream>"`
- `SRC_URI = "git://github.com/wb2osz/direwolf.git;protocol=https;branch=master;tag=${PV}"`
- `SRCREV` pinned
- `inherit cmake pkgconfig`, `DEPENDS = "alsa-lib"`
- `EXTRA_OECMAKE = "-DUNITTEST=OFF"` (only if the option exists upstream; verify in `CMakeLists.txt`)
- `do_install:append()` removes `${sysconfdir}/udev`, `${datadir}/applications`, `${datadir}/pixmaps` and `${datadir}/doc/direwolf`
- Before writing the recipe, check whether an existing layer in the kas config has one (`meta-oe`/`meta-multimedia`/`meta-networking` at branch `wrynose`). If one exists, use it with a `PREFERRED_VERSION` and drop this file.

- [ ] **Step 1: Write the failing static tests**

```python
def test_direwolf_recipe_pinned():
    txt = _read(*_DW)
    assert "AUTOREV" not in txt and re.search(r'^SRCREV\s*=\s*"[0-9a-f]{40}"', txt, re.M)
    assert os.path.basename(_path(*_DW)) == "direwolf_1.8.1.bb"
def test_direwolf_recipe_minimal():
    lines = _code_lines(_read(*_DW))
    assert 'DEPENDS = "alsa-lib"' in [l.strip() for l in lines]
    assert any("udev" in l and "rm" in l for l in lines)
def test_image_installs_direwolf_and_decoder():
    assert re.search(r'IMAGE_INSTALL:append\s*=\s*".*\bdirewolf\b.*\boe5xrx-decoder\b', _read(*_IMAGE))
```

- [ ] **Step 2:** Run `cd tests/ota-integration && python -m pytest -m unit test_decoder_static.py -q`. Expected: FAIL.
- [ ] **Step 3:** Write the recipe and the image line.
- [ ] **Step 4:** Run the static tests (PASS). Then build: `kas shell qemux86-64.yml -c "bitbake direwolf"` on the build server (`ydev remote-build`, warm ~10 min). Expected: build OK, `oe-pkgdata-util list-pkg-files direwolf` shows only `/usr/bin/direwolf`.
- [ ] **Step 5: Commit** `git commit -m "direwolf 1.8.1 recipe (RX decoder for passive APRS)"`

---

### Task 2: `oe5xrx-decoder` recipe: user, template unit, wrapper

**Files:**
- Create:
  - `meta-oe5xrx-remotestation/recipes-multimedia/oe5xrx-decoder/oe5xrx-decoder_1.0.bb`
  - `meta-oe5xrx-remotestation/recipes-multimedia/oe5xrx-decoder/files/oe5xrx-decoder@.service`
  - `meta-oe5xrx-remotestation/recipes-multimedia/oe5xrx-decoder/files/oe5xrx-decoder-run`
- Test: `tests/ota-integration/test_decoder_static.py` (extend)

**Interfaces:**
- Consumes: package `direwolf` (Task 1); `pw-record` from `pipewire-tools` (already in `oe5xrx-audio-system` RDEPENDS); group `pipewire` (created by the pipewire recipe).
- Produces:
  - User and group `oe5xrx-decoder`.
  - Unit `${systemd_system_unitdir}/oe5xrx-decoder@.service`. It is a template, **not** enabled (`SYSTEMD_AUTO_ENABLE = "disable"`, no `SYSTEMD_SERVICE` instance). The agent starts instances.
  - Wrapper `${libexecdir}/oe5xrx/oe5xrx-decoder-run <instance>`.

Recipe essentials:
- `inherit systemd useradd allarch`
- `USERADD_PACKAGES = "${PN}"`, `USERADD_DEPENDS = "pipewire"`
- `USERADD_PARAM:${PN} = "--system --no-create-home --home-dir /nonexistent --shell /sbin/nologin --user-group --groups pipewire oe5xrx-decoder"`
- `RDEPENDS:${PN} = "bash direwolf pipewire-tools"`

Unit content (directive list; comments explain each block like `oe5xrx-pipewire.service` does):

```ini
[Unit]
Description=OE5XRX decoder %i
After=oe5xrx-pipewire.service
Wants=oe5xrx-pipewire.service
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User=oe5xrx-decoder
Group=oe5xrx-decoder
SupplementaryGroups=pipewire
Environment=PIPEWIRE_RUNTIME_DIR=/run/pipewire
EnvironmentFile=/run/oe5xrx/decoder/%i.env
RuntimeDirectory=oe5xrx-decoder/%i
ExecStart=/usr/libexec/oe5xrx/oe5xrx-decoder-run %i
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
IPAddressDeny=any
IPAddressAllow=localhost
MemoryMax=96M
CPUQuota=50%
StandardOutput=null
StandardError=journal
LogLevelMax=notice
LogRateLimitIntervalSec=30s
LogRateLimitBurst=20
```

Wrapper (`#!/bin/bash`, `set -euo pipefail`):
1. Validate `$1` matches `^[a-z][a-z0-9]{0,15}-slot[0-9]{1,3}$`. Validate the env: `OE5XRX_RX_NODE` `^[A-Za-z0-9._:@/+-]{1,200}$`, `OE5XRX_KISS_PORT` `^[0-9]{4,5}$`, `OE5XRX_SAMPLE_RATE` `^[0-9]{4,6}$`, and that `OE5XRX_DECODER` equals the instance prefix. On any mismatch, print one line to stderr and exit 2.
2. `case "$OE5XRX_DECODER" in aprs)`: write `${RUNTIME_DIRECTORY:-$OE5XRX_TEST_RUNDIR}/direwolf.conf` with:

   ```
   ADEVICE stdin null
   ARATE <rate>
   ACHANNELS 1
   CHANNEL 0
   MODEM 1200
   KISSPORT <port>
   AGWPORT 0
   ```

   Then `pw-record --target "$OE5XRX_RX_NODE" --rate "$OE5XRX_SAMPLE_RATE" --channels 1 --format s16 - | direwolf -c <conf> -t 0 -q hd -r "$OE5XRX_SAMPLE_RATE" -`.
3. `*)`: unknown decoder → exit 2.
4. Binaries come from `PATH` (tests put fakes first). No `eval`, all expansions quoted.

- [ ] **Step 1: Write the failing static and behaviour tests**

```python
def test_unit_sandbox_and_log_limits():
    d = _directives(_read(*_UNIT))   # helper: {key: value} from code lines
    for k, v in {"User": "oe5xrx-decoder", "NoNewPrivileges": "yes", "ProtectSystem": "strict",
                 "PrivateTmp": "yes", "LogLevelMax": "notice", "StandardOutput": "null",
                 "Restart": "on-failure", "MemoryMax": "96M", "CPUQuota": "50%"}.items():
        assert d[k] == v
    assert "LogRateLimitIntervalSec" in d and "LogRateLimitBurst" in d
def test_unit_network_sandbox():
    d = _directives(_read(*_UNIT)); assert d["IPAddressDeny"] == "any" and d["IPAddressAllow"] == "localhost"
def test_unit_template_not_enabled():
    assert 'SYSTEMD_AUTO_ENABLE = "disable"' in _read(*_RECIPE)
def test_decoder_user_in_pipewire_group():
    r = _read(*_RECIPE); assert "USERADD_DEPENDS" in r and "--groups pipewire" in r
def test_wrapper_syntax():
    subprocess.run(["bash", "-n", _path(*_WRAPPER)], check=True)
def test_wrapper_writes_rx_only_conf(tmp_path):
    # fake pw-record (prints nothing, exit 0) + fake direwolf (copies -c conf to tmp, exit 0) on PATH
    # env OE5XRX_DECODER=aprs ... OE5XRX_TEST_RUNDIR=tmp -> conf contains "ADEVICE stdin null", "KISSPORT 8101", "AGWPORT 0"
    # and none of "PTT", "BEACON", "IGSERVER", "DIGIPEAT"
@pytest.mark.parametrize("bad", ["node\nKISSPORT 1", "a;b", "$(id)", ""])
def test_wrapper_rejects_bad_env(tmp_path, bad): ...   # exit code 2, fakes never invoked
def test_wrapper_pipeline_failure_propagates(tmp_path): ...  # fake pw-record exit 1 -> wrapper exit != 0
```

- [ ] **Step 2:** Run `cd tests/ota-integration && python -m pytest -m unit test_decoder_static.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3:** Write the recipe, unit and wrapper. Run `shellcheck` on the wrapper if it is installed.
- [ ] **Step 4:** Run the static tests (PASS). Then `bitbake oe5xrx-remotestation-image` for `qemux86-64`. Expected: build OK. `/etc/group` in the rootfs (`tmp/work/*/oe5xrx-remotestation-image/*/rootfs/etc/group`) lists `oe5xrx-decoder` in `pipewire`.
- [ ] **Step 5: Commit** `git commit -m "oe5xrx-decoder: sandboxed template unit + wrapper + decoder user"`

---

### Task 3: Spike + on-device verification on test station .211 (HIL, manual)

This task covers spec §10 risks 1 and 2. It needs the user's go for the station. It produces **no code**; the findings go into the PR body and project memory `feature/passive-aprs/spike`.

- [ ] **Step 1:** Flash the dev image from Task 2 onto .211 (normal OTA path, or the dev image), with the agent at the main plan's branch head (dev-agent-mount).
- [ ] **Step 2:** On the station: `id oe5xrx-decoder` shows group `pipewire`. `zcat /proc/config.gz | grep CGROUP_BPF` (or `/boot/config-*`) shows `=y`.
- [ ] **Step 3:** Open the control page and listen (Opus RX running). Write the env file by hand for slot 1 (`OE5XRX_RX_NODE` from `pw-dump`, as the agent's `resolve_node` finds it) and run `systemctl start oe5xrx-decoder@aprs-slot1`. Expected: Opus RX keeps playing without dropouts for 10 min, the unit stays `active`, and `kissutil`-like reading of `127.0.0.1:8101` (e.g. `nc 127.0.0.1 8101 | xxd`) shows frames while 144.800 has traffic.
- [ ] **Step 4:** From another host, `nc -vz <station-ip> 8101` must fail. `journalctl -u 'oe5xrx-decoder@*' --since -10min | wc -l` must be small (start lines only, no packet lines).
- [ ] **Step 5:** Decode rate: let it run for 1 h with squelch 0 at the APRS profile volume. Count frames and compare with aprs.fi "heard by nearby iGates" for the same window. Record packets/h and pick the `volume` value for the APRS profile default (feeds back into the main plan's `PassiveServiceConfig.profile`).
- [ ] **Step 6:** `systemctl stop 'oe5xrx-decoder@*'`. Reset the station to the release image if a dev image was used.

---

### Task 4: Agent SRCREV bump

Do this only after the station-manager PR (main plan) is merged to `main`.

**Files:**
- Modify: `meta-oe5xrx-remotestation/recipes-core/station-agent/station-agent_0.1.0.bb` (via the script)

- [ ] **Step 1:** Run `scripts/pin-station-agent.sh` (defaults to station-manager `main@HEAD`). Expected: only the `SRCREV` line changes.
- [ ] **Step 2:** Run the existing preflight and static tests: `cd tests/ota-integration && python -m pytest -m unit -q`. Expected: PASS.
- [ ] **Step 3:** Run the CI build + boot test via the PR (`pr.yml`). Expected: green. The audio agent e2e (`test_audio_agent_e2e.py`) must stay green: the agent now sends `features` in the inventory, and nothing starts a decoder by itself.
- [ ] **Step 4: Commit** `git commit -m "station-agent: bump to <sha> (decoder host for passive APRS)"`

---

### Task 5: servers repo: `aprs-igate` service

**Files:**
- Modify: `services/station_manager/docker-compose.yml` (insert after the `repeater-sync` block, :298-308)

- [ ] **Step 1:** Add:

```yaml
  # APRS-IS-Gateway der passiven Dienste: eine TCP-Verbindung pro aktiver
  # Station zu rotate.aprs2.net:14580 (ausgehend), Beacons, Retention der
  # APRS-Rohpakete. Genau EINE Instanz (keine Replikas) — sonst doppelte Logins.
  aprs-igate:
    image: ghcr.io/oe5xrx/station-manager:latest
    restart: unless-stopped
    command: python manage.py aprs_igate
    environment: *station-manager-env
    networks:
      - station_manager-internal
    depends_on:
      init:
        condition: service_completed_successfully
```

- [ ] **Step 2:** Run `docker compose -f services/station_manager/docker-compose.yml config -q`. Expected: no output, exit 0.
- [ ] **Step 3: Commit + PR** `git commit -m "station_manager: aprs-igate worker service"`. Merge only after the station-manager release containing `aprs_igate` is deployed. The service crash-loops on an older image, so the deploy order is server image → this PR.

---

## Deploy order (spec §9)

1. station-manager release (feature dormant: `PassiveServiceConfig.enabled=False`).
2. servers PR (Task 5).
3. linux-image release with Direwolf + decoder unit + agent bump (Tasks 1, 2, 4), rolled out like any OTA.
4. Admin enables the service and opts in .211 first (24 h soak, spec §13), then further stations.
