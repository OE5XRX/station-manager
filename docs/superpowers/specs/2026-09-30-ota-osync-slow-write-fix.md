# OTA-Apply: O_SYNC entfernen — Fix für Watchdog-Reset auf echtem CM4

**Status:** LOCKED (Root Cause auf echtem CM4 bewiesen, 2026-09-30)
**Scope:** NUR `station_agent/ota.py::install_to_slot` (+ Unit-Test). Kein Server, kein Bootloader-Protokoll.

## Problem / Root Cause (bewiesen auf CM4 192.168.88.211)

`install_to_slot` öffnet das Ziel-Slot-Blockdevice mit `os.open(dev, O_WRONLY | O_SYNC)`.
`O_SYNC` zwingt **jeden** 1-MiB-`os.write`, physisch auf die SD durchzuschreiben.

Messung auf dem CM4 (`dd`, 100 MiB nach /mnt/data): **buffered ~5.0 MB/s, O_SYNC ~3.0 MB/s.**
Die ~2 GB rootfs brauchen mit O_SYNC also **~11–14 min**, wobei der SD-Bus voll saturiert wird.
Der OTA-Apply läuft synchron in der Heartbeat-Hauptloop → der Agent blockiert minutenlang.
Die I/O-Sättigung starved schließlich PID1 (systemd), das den **BCM2835-HW-Watchdog
(`RuntimeWatchdogUSec=14s`, `/dev/watchdog0`)** nicht mehr rechtzeitig pettet → **HW-Reset
mitten im Write**. Der A/B-Trial wird nie armiert (`upgrade_available` bleibt 0) → Reboot
zurück auf den unveränderten alten Slot. Nicht-deterministisch, weil die Write-Zeit um die
Starvation-Schwelle (~7 min beobachtet) pendelt.

Unter QEMU ist die virtuelle Disk schnell → Write in Sekunden → keine Starvation → grün.
Klassisches „sim-green ≠ real-CM4-green".

Zusatz: `install_to_slot` macht am Ende ohnehin `os.fsync(fd)` — O_SYNC pro-Write ist
doppelt gemoppelt und rein schädlich.

## Fix (LOCKED)

In `install_to_slot`:
- **`O_SYNC` entfernen** → `os.open(dev, os.O_WRONLY)` (buffered write).
- **Periodisch** (alle `_SYNC_INTERVAL`, 64 MiB) `os.fdatasync(fd)` +
  `os.posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` — begrenzt Dirty-Pages/Writeback-Spikes
  und hält den Page-Cache klein (2 GB Write soll ihn nicht thrashen), so bleibt PID1 responsiv.
- **Finaler `os.fsync(fd)`** bleibt (volle Durability + surfaced I/O-Fehler).

Ergebnis: Write bleibt schnell genug und System responsiv → Watchdog feuert nicht →
Trial wird armiert → OTA klappt auf echter HW.

## Nicht Teil dieses Fixes (Follow-ups)
- OTA-Apply aus der Heartbeat-Hauptloop in einen Thread ziehen (Heartbeats während Apply).
- Auffällig langsame SD (~5 MB/s) — evtl. schwache/degradierte Karte, separat prüfen.
- Warum ein früher erfolgreicher b-Boot nicht committed blieb (Commit/Verify-Pfad), separat.

## Verifikation
- Unit: `install_to_slot` öffnet NICHT mit O_SYNC, synct (fdatasync/fsync) mind. einmal,
  Round-trip-Bytes korrekt; bestehende Tests (truncated/multi-stream/EIO) bleiben grün.
- **Auf echtem CM4 (Pflicht, sim reicht nicht):** patched Write-Pfad schreibt die rootfs
  nach dem inaktiven Slot ohne HW-Reset, in deutlich <7 min; Trial wird armiert.
