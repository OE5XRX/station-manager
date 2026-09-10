from textwrap import dedent


def render_config(*, server_url: str, station_id: int) -> str:
    # config.yml is a CONFFILE preserved across OTA, so existing stations pick up changes
    # to this template only via re-provisioning (or a manual on-device edit). The control
    # channel is always on (server_url is required) and audio is auto-detected on-device,
    # so neither has a flag here anymore.
    return dedent(
        f"""\
        server_url: {server_url}
        station_id: {station_id}
        ed25519_key_path: /etc/stationagent/device_key.pem
        heartbeat_interval: 60
        ota_check_interval: 5
        download_dir: /tmp/station-agent
        log_level: INFO
        terminal_enabled: true
        terminal_shell: /bin/sh
        bootloader: auto
        """
    )
