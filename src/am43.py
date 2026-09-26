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


def build_position_query_frame() -> bytes:
    """Build position query frame."""
    return build_frame([CMD_HEADER, CMD_QUERY_POSITION, 0x01, 0x01])


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

    cmd = data[1] if len(data) > 1 else None
    valid_checksum = verify_frame_checksum(data)
    if not valid_checksum and cmd != CMD_SET_POSITION:
        logger.warning("Notification received with invalid checksum: %s", raw_hex)

    header = data[0]
    if header not in (RESP_HEADER, CMD_HEADER):
        logger.debug("Received frame with non-standard header 0x%02x: %s", header, raw_hex)

    cmd = data[1]
    position: int | None = None
    battery: int | None = None
    state: str | None = None

    # Position notification (0xA1 notify, 0xA7 position query, 0xA8).
    # Typical A7 reply: [0x9A, 0xA7, 0x07, <status/flags>, <speed/param>, <actual_pos>, ...]
    # Example: 9aa7070f3234... -> byte 3=0x0f, byte 4=0x32 (50 speed), byte 5=0x34 (52% actual pos)
    # Typical A1 frame: [0x9A, 0xA1, 0x07, <status>, <speed>, <actual_pos>, ...]
    if cmd in (0xA1, CMD_QUERY_POSITION, 0xA8):
        if len(data) >= 7:
            # Long format with flags and speed/param: position is at byte 5 (index 5)
            candidate = data[5]
            if 0 <= candidate <= 100:
                position = candidate
            elif 0 <= data[4] <= 100:
                position = data[4]
        elif len(data) == 6:
            candidate = data[4]
            if 0 <= candidate <= 100:
                position = candidate
        elif len(data) == 5:
            # Short format: [header, cmd, len, pos, checksum]
            candidate = data[3]
            if 0 <= candidate <= 100:
                position = candidate

    # Command ACK / status notification (cmd 0x0D)
    elif cmd == CMD_SET_POSITION:
        # Some motors echo ACK with status 0x5A or an updated position
        if len(data) >= 4 and data[3] == RESP_HEADER:
            # Command acknowledged (ACK 0x5A)
            state = "acknowledged"

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
            state = "closed"
        elif position == 100:
            state = "open"
        else:
            state = "open"

    return DecodedNotification(
        raw_hex=raw_hex,
        cmd=cmd,
        position=position,
        battery=battery,
        state=state,
    )
