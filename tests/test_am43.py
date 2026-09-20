"""Unit tests for AM43 protocol frame generation and decoding."""

import unittest
from src.am43 import (
    calculate_xor_checksum,
    build_set_position_frame,
    build_open_frame,
    build_close_frame,
    build_stop_frame,
    build_battery_query_frame,
    verify_frame_checksum,
    parse_notification,
)


class TestAm43Protocol(unittest.TestCase):
    def test_xor_checksum(self):
        # 50% position payload: [0x9a, 0x0d, 0x01, 0x32]
        # 0x9a ^ 0x0d ^ 0x01 ^ 0x32 = 0xa4
        payload = [0x9A, 0x0D, 0x01, 0x32]
        expected_cs = 0x9A ^ 0x0D ^ 0x01 ^ 0x32
        self.assertEqual(calculate_xor_checksum(payload), expected_cs)

    def test_build_set_position_frame(self):
        # Position 50% (0x32)
        frame = build_set_position_frame(50)
        self.assertEqual(len(frame), 5)
        self.assertEqual(frame[0], 0x9A)
        self.assertEqual(frame[1], 0x0D)
        self.assertEqual(frame[2], 0x01)
        self.assertEqual(frame[3], 50)
        self.assertTrue(verify_frame_checksum(frame))

    def test_build_action_frames(self):
        open_frame = build_open_frame()
        self.assertEqual(open_frame[0], 0x9A)
        self.assertEqual(open_frame[1], 0x0A)
        self.assertEqual(open_frame[2], 0x01)
        self.assertEqual(open_frame[3], 0x00)
        self.assertTrue(verify_frame_checksum(open_frame))

        close_frame = build_close_frame()
        self.assertEqual(close_frame[3], 0x01)
        self.assertTrue(verify_frame_checksum(close_frame))

        stop_frame = build_stop_frame()
        self.assertEqual(stop_frame[3], 0x02)
        self.assertTrue(verify_frame_checksum(stop_frame))

    def test_build_battery_frame(self):
        # Check battery frame [0x9a, 0xa2, 0x01, 0x01, 0x38]
        frame = build_battery_query_frame()
        self.assertEqual(frame.hex(), "9aa2010138")
        self.assertTrue(verify_frame_checksum(frame))

    def test_parse_position_notification(self):
        # Simulated position notification: 0x5a, 0x0d, 0x01, 30%, checksum
        payload = [0x5A, 0x0D, 0x01, 30]
        cs = calculate_xor_checksum(payload)
        frame = bytes(payload + [cs])

        res = parse_notification(frame)
        self.assertEqual(res.position, 30)
        self.assertEqual(res.state, "open")

    def test_parse_battery_notification(self):
        # Simulated battery notification: 0x5a, 0xa2, 0x01, 85%, checksum
        payload = [0x5A, 0xA2, 0x01, 85]
        cs = calculate_xor_checksum(payload)
        frame = bytes(payload + [cs])

        res = parse_notification(frame)
        self.assertEqual(res.battery, 85)


if __name__ == "__main__":
    unittest.main()
