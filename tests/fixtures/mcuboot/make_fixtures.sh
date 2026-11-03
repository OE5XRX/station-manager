#!/usr/bin/env bash
# Regenerates the MCUboot KEYHASH fixtures. Dev key = MCUboot's public root-ec-p256.pem
# (https://raw.githubusercontent.com/mcu-tools/mcuboot/main/root-ec-p256.pem,
#  sha256 1734d7ee76344fbf2b1731f49baa5e1ced9bea56b66fe84703b1a492e0a13614).
# other-key.pem is a throwaway test key (imgtool keygen -t ecdsa-p256), used nowhere else.
set -euo pipefail
cd "$(dirname "$0")"
PAYLOAD="$(mktemp)"
trap 'rm -f "$PAYLOAD"' EXIT
head -c 2048 /dev/zero | tr '\0' '\125' > "$PAYLOAD"
IMG="uvx --from imgtool==2.5.0 imgtool"
$IMG sign --key root-ec-p256.pem --header-size 0x200 --pad-header --align 4 \
  --version 1.2.3 --slot-size 0x20000 "$PAYLOAD" dev-signed.bin
$IMG sign --key other-key.pem   --header-size 0x200 --pad-header --align 4 \
  --version 1.2.3 --slot-size 0x20000 "$PAYLOAD" other-signed.bin
