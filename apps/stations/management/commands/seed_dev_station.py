"""Legt eine Dev-Station + statischen Device-Key an und druckt die Agent-Config.
NUR unter DEBUG/Dev-Settings — statische Keys dürfen nie in Prod (Spec §4)."""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.api.models import DeviceKey
from apps.stations.models import Station


class Command(BaseCommand):
    help = "Seed a dev station + static device key and print the agent config.yml."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="Dev Station")
        parser.add_argument("--callsign", default="OE5XRX")
        parser.add_argument("--server-url", default="http://10.0.2.2:8000")
        parser.add_argument("--key-out", default="./dev-device-key.pem")

    def handle(self, *args, name, callsign, server_url, key_out, **opts):
        if not settings.DEBUG:
            raise CommandError("seed_dev_station refuses to run without DEBUG (dev only).")

        station, _ = Station.objects.get_or_create(
            name=name, defaults={"callsign": callsign}
        )
        key = DeviceKey.objects.filter(station=station).first()
        if key is None:
            private_pem, public_b64 = DeviceKey.generate_keypair()
            key = DeviceKey.objects.create(station=station, current_public_key=public_b64)
            # Create with 0600 perms atomically — a private key must never be
            # world-readable, not even for a moment (no write-then-chmod window).
            fd = os.open(key_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, private_pem)
            finally:
                os.close(fd)
            self.stderr.write(f"Wrote private key to {key_out}")
        else:
            self.stderr.write("DeviceKey already exists; reusing (private key not re-shown).")

        self.stdout.write("# --- agent config.yml (dev) ---")
        self.stdout.write(f"server_url: {server_url}")
        self.stdout.write(f"station_id: {station.id}")
        self.stdout.write(f"ed25519_key_path: {key_out}")
        self.stdout.write("log_level: DEBUG")
        self.stdout.write("trace_serial: true")
        self.stdout.write("slot_discovery_enabled: true")
        self.stdout.write("control_enabled: true")
