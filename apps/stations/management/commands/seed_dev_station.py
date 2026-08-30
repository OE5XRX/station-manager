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

        # Station.name is NOT unique, so get_or_create(name=...) would raise
        # MultipleObjectsReturned on a dev DB that already has same-named rows.
        # Reuse the first match (idempotent) and only create when none exists.
        station = Station.objects.filter(name=name).first()
        if station is None:
            station = Station.objects.create(name=name, callsign=callsign)

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
        elif os.path.exists(key_out):
            self.stderr.write("DeviceKey already exists; reusing (private key not re-shown).")
        else:
            # DB has the key but the private-key file is gone (e.g. cleaned working
            # dir). The private key is only written once at creation, so the config
            # below would point at a missing file — say so instead of failing silently.
            self.stderr.write(
                f"WARNING: DeviceKey exists in DB but no private key at {key_out}. "
                "The private key was only shown once at creation. Delete the DeviceKey "
                "and re-run to regenerate, or restore the key file — the printed "
                "ed25519_key_path currently points at a nonexistent file."
            )

        self.stdout.write("# --- agent config.yml (dev) ---")
        self.stdout.write(f"server_url: {server_url}")
        self.stdout.write(f"station_id: {station.id}")
        self.stdout.write(f"ed25519_key_path: {key_out}")
        self.stdout.write("log_level: DEBUG")
        self.stdout.write("trace_serial: true")
        self.stdout.write("slot_discovery_enabled: true")
        self.stdout.write("control_enabled: true")
