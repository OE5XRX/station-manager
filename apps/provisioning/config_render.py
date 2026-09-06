from textwrap import dedent


def render_config(*, server_url: str, station_id: int, audio_enabled: bool = False) -> str:
    # audio_enabled is emitted explicitly: the agent defaults it to False ("Audio channel
    # disabled"), so a station only runs the audio engine when provisioning flags it on.
    # config.yml is a CONFFILE preserved across OTA, so existing stations pick up a change
    # to this flag only via re-provisioning (or a manual on-device edit).
    audio_flag = "true" if audio_enabled else "false"
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
        control_enabled: true
        audio_enabled: {audio_flag}
        """
    )
