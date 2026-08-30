"""Rohbyte-Hexdump für Serial-I/O-Debugging (Fast-Dev-Loop, `trace_serial` config)."""
import logging


def hexdump(direction: str, data: bytes) -> str:
    return f"{direction} {len(data)} bytes: {data.hex()}"


def log_io(logger: logging.Logger, direction: str, data: bytes, enabled: bool) -> None:
    if enabled and data:
        logger.debug("serial %s", hexdump(direction, data))
