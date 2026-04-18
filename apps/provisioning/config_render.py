from textwrap import dedent


def render_config(*, server_url: str, station_id: int) -> str:
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
