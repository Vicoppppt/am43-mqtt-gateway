"""AM43 protocol encoding and decoding helpers.

Reference specifications:
- Write characteristic (GATT): 0000fe52-0000-1000-8000-00805f9b34fb
- Notify characteristic (GATT): 0000fe51-0000-1000-8000-00805f9b34fb
- Frame format (Compact without PIN):
    [0x9A, <CMD>, <LEN>, <DATA...>, <CHECKSUM_XOR>]
- Position convention:
    0 = 100% Open (Ouvert)
    100 = 100% Closed (Fermé)
"""

from __future__ import annotations
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

WRITE_CHAR_UUID = "0000fe52-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fe51-0000-1000-8000-00805f9b34fb"

# Headers
CMD_HEADER = 0x9A
RESP_HEADER = 0x5A

# Command IDs
CMD_ACTION = 0x0A
CMD_SET_POSITION = 0x0D
CMD_QUERY_BATTERY = 0xA2
CMD_QUERY_POSITION = 0xA7

# Action types
ACTION_OPEN = 0x00
ACTION_CLOSE = 0x01
ACTION_STOP = 0xCC


def calculate_xor_checksum(payload: bytes | list[int]) -> int:
    """Calculate the XOR checksum of a sequence of bytes."""
    checksum = 0
    for byte in payload:
        checksum ^= byte
    return checksum


def build_frame(payload: list[int]) -> bytes:
    """Append the XOR checksum to payload and return raw bytes."""
    cs = calculate_xor_checksum(payload)
    return bytes(payload + [cs])


def build_set_position_frame(position: int) -> bytes:
    """Build frame to set position (0 = fully open, 100 = fully closed)."""
    clamped = max(0, min(100, int(position)))
    return build_frame([CMD_HEADER, CMD_SET_POSITION, 0x01, clamped])


def build_action_frame(action: int) -> bytes:
    """Build action frame (ACTION_OPEN, ACTION_CLOSE, ACTION_STOP)."""
    return build_frame([CMD_HEADER, CMD_ACTION, 0x01, action])


def build_open_frame() -> bytes:
    """Convenience helper to build OPEN command (0% position)."""
    return build_set_position_frame(0)


def build_close_frame() -> bytes:
    """Convenience helper to build CLOSE command (100% position)."""
    return build_set_position_frame(100)


def build_stop_frame() -> bytes:
    """Convenience helper to build STOP command."""
    return build_action_frame(ACTION_STOP)


def build_battery_query_frame() -> bytes:
    """Build battery query frame [0x9A, 0xA2, 0x01, 0x01, 0x38]."""
    return build_frame([CMD_HEADER, CMD_QUERY_BATTERY, 0x01, 0x01])


def verify_frame_checksum(data: bytes) -> bool:
    """Verify if the last byte matches the XOR checksum of all preceding bytes."""
    if len(data) < 2:
        return False
    return calculate_xor_checksum(data[:-1]) == data[-1]


class DecodedNotification(NamedTuple):
    raw_hex: str
    cmd: int | None = None
    position: int | None = None
    battery: int | None = None
    state: str | None = None


def parse_notification(data: bytes) -> DecodedNotification:
    """Parse motor notification received on NOTIFY_CHAR (0xfe51)."""
    raw_hex = data.hex()
    if len(data) < 4:
        return DecodedNotification(raw_hex=raw_hex)

    if not verify_frame_checksum(data):
        logger.warning("Notification received with invalid checksum: %s", raw_hex)

    header = data[0]
    if header != RESP_HEADER:
        logger.debug("Received frame with non-standard header 0x%02x: %s", header, raw_hex)

    cmd = data[1]
    position: int | None = None
    battery: int | None = None
    state: str | None = None

    # Position notification (typically cmd 0x0D, 0xA8, 0xA1, 0xA7)
    if cmd in (CMD_SET_POSITION, 0xA8, 0xA1, CMD_QUERY_POSITION):
        if len(data) >= 5:
            # Position byte is typically at index 3 or 4
            pos_candidate = data[3]
            if 0 <= pos_candidate <= 100:
                position = pos_candidate

    # Battery notification (cmd 0xA2)
    elif cmd == CMD_QUERY_BATTERY:
        # Depending on firmware, battery % is at byte 7 or byte 3/4
        # Format: 5a a2 06 01 01 01 01 <battery_percent> <checksum>
        # or compact: 5a a2 01 <battery_percent> <checksum>
        if len(data) >= 8:
            candidate = data[7]
            if 0 <= candidate <= 100:
                battery = candidate
        elif len(data) >= 5:
            candidate = data[3]
            if 0 <= candidate <= 100:
                battery = candidate

    if position is not None:
        if position == 0:
            state = "open"
        elif position == 100:
            state = "closed"
        else:
            state = "open"

    return DecodedNotification(
        raw_hex=raw_hex,
        cmd=cmd,
        position=position,
        battery=battery,
        state=state,
    )
