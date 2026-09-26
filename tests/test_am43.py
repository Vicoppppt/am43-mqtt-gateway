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
        self.assertEqual(open_frame[1], 0x0D)
        self.assertEqual(open_frame[3], 0x00)
        self.assertTrue(verify_frame_checksum(open_frame))

        close_frame = build_close_frame()
        self.assertEqual(close_frame[0], 0x9A)
        self.assertEqual(close_frame[1], 0x0D)
        self.assertEqual(close_frame[3], 100)
        self.assertTrue(verify_frame_checksum(close_frame))

        stop_frame = build_stop_frame()
        self.assertEqual(stop_frame[1], 0x0A)
        self.assertEqual(stop_frame[3], 0xCC)
        self.assertTrue(verify_frame_checksum(stop_frame))

    def test_build_battery_frame(self):
        # Check battery frame [0x9a, 0xa2, 0x01, 0x01, 0x38]
        frame = build_battery_query_frame()
        self.assertEqual(frame.hex(), "9aa2010138")
        self.assertTrue(verify_frame_checksum(frame))

    def test_parse_position_notification(self):
        # 1. Standard AM43 telemetry frame (0xA1): [0x9a, 0xa1, 0x07, <status>, <pos=85>, ...]
        payload = [0x9A, 0xA1, 0x07, 0x0F, 85, 0x00, 0x00]
        cs = calculate_xor_checksum(payload)
        frame = bytes(payload + [cs])

        res = parse_notification(frame)
        self.assertEqual(res.position, 85)
        self.assertEqual(res.state, "open")

        # 2. Position closed (100)
        payload_closed = [0x9A, 0xA1, 0x07, 0x0F, 100, 0x00, 0x00]
        cs_closed = calculate_xor_checksum(payload_closed)
        frame_closed = bytes(payload_closed + [cs_closed])
        res_closed = parse_notification(frame_closed)
        self.assertEqual(res_closed.position, 100)
        self.assertEqual(res_closed.state, "open")

        # 3. Position query reply (0xA7)
        payload_query = [0x9A, 0xA7, 0x07, 0x00, 0, 0x00, 0x00]
        cs_query = calculate_xor_checksum(payload_query)
        frame_query = bytes(payload_query + [cs_query])
        res_query = parse_notification(frame_query)
        self.assertEqual(res_query.position, 0)
        self.assertEqual(res_query.state, "closed")

    def test_parse_set_position_ack(self):
        # ACK frame received: 9a 0d 01 5a 31
        frame = bytes.fromhex("9a0d015a31")
        res = parse_notification(frame)
        self.assertEqual(res.cmd, 0x0D)
        self.assertEqual(res.state, "acknowledged")

    def test_parse_battery_notification(self):
        # Simulated battery notification: 0x5a, 0xa2, 0x01, 85%, checksum
        payload = [0x5A, 0xA2, 0x01, 85]
        cs = calculate_xor_checksum(payload)
        frame = bytes(payload + [cs])

        res = parse_notification(frame)
        self.assertEqual(res.battery, 85)


if __name__ == "__main__":
    unittest.main()
