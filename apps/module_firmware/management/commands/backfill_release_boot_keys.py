from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.module_firmware import storage
from apps.module_firmware.mcuboot import parse_boot_key_hash
from apps.module_firmware.models import ModuleFirmwareRelease


class Command(BaseCommand):
    help = "Fill ModuleFirmwareRelease.boot_key_hash from stored images where still empty."

    def handle(self, *args, **options):
        qs = ModuleFirmwareRelease.all_objects.filter(boot_key_hash="").exclude(storage_key="")
        total = updated = 0
        for rel in qs.iterator():
            total += 1
            try:
                with storage.open_stream(rel.storage_key) as fh:
                    blob = fh.read()
            except Exception as exc:  # one bad object must not abort the run
                self.stderr.write(f"skip {rel.pk} ({rel.storage_key}): {exc}")
                continue
            prefix = parse_boot_key_hash(blob)
            if prefix:
                rel.boot_key_hash = prefix
                rel.save(update_fields=["boot_key_hash"])
                updated += 1
        self.stdout.write(f"updated {updated} of {total}")
