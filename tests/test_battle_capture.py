from contextlib import closing
from io import BytesIO
import struct
from threading import Event
import unittest
from unittest.mock import Mock, patch

from battle_capture import BattleFrameStream, _read_frame


def packet(pixels, *, width=2, height=1, stride=6, bpp=3):
    return struct.pack("<IIIII", 16 + stride * height, width, height, stride, bpp) + pixels


class BattleFrameTests(unittest.TestCase):
    def test_read_native_rgb_with_row_padding(self):
        payload = bytes.fromhex("ff0000 00ff00 0a0b 0000ff ffffff 0c0d")
        frame = _read_frame(BytesIO(packet(payload, height=2, stride=8)), (2, 2))
        self.assertIsNotNone(frame)
        _, image = frame
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (2, 2))
        self.assertEqual(image.tobytes(), bytes.fromhex("ff0000 00ff00 0000ff ffffff"))

    def test_reject_invalid_headers_and_incomplete_frames(self):
        for data in (
            b"usage: screenrecord",
            packet(b"\x00" * 6, width=1),
            packet(b"\x00" * 6, stride=5),
            packet(b"\x00" * 6, bpp=4),
            packet(b"", stride=64 * 1024 * 1024 + 1),
            packet(b"\x00" * 5),
            b"\x00" * 4 + packet(b"\x00" * 6)[4:],
        ):
            with self.subTest(data=data):
                self.assertIsNone(_read_frame(BytesIO(data), (2, 1)))

    def test_stream_returns_only_newest_frame_once(self):
        eof = Event()

        class RecordedPipe(BytesIO):
            def read(self, size):
                data = super().read(size)
                if not data:
                    eof.set()
                return data

        first, latest = bytes.fromhex("010000 010000"), bytes.fromhex("020000 020000")
        process = Mock()
        process.poll.return_value = 0
        process.stdout = RecordedPipe(packet(first) + packet(latest))
        with patch("battle_capture.subprocess.Popen", return_value=process) as launch:
            with closing(BattleFrameStream("adb", "emulator-5554", (2, 1))) as stream:
                self.assertTrue(eof.wait(timeout=1))
                _, image = stream.next_frame(timeout=1)
                self.assertEqual(image.tobytes(), latest)
                self.assertIsNone(stream.next_frame(timeout=0))
        self.assertEqual(launch.call_args.args[0][4:], [
            "screenrecord", "--size", "2x1", "--output-format=frames", "--time-limit", "180", "-",
        ])
        process.kill.assert_not_called()

    def test_timeout_and_close_of_a_stalled_stream(self):
        stop = Event()
        process = Mock()
        process.poll.return_value = None
        process.kill.side_effect = stop.set

        class StalledPipe(BytesIO):
            def read(self, size):
                stop.wait(timeout=2)
                return b""

        process.stdout = StalledPipe()
        with patch("battle_capture.subprocess.Popen", return_value=process):
            with closing(BattleFrameStream("adb", "device", (2, 1))) as stream:
                self.assertIsNone(stream.next_frame(timeout=0))
            self.assertIsNone(stream.next_frame(timeout=0))
            stream.close()
        process.kill.assert_called_once_with()
        self.assertTrue(process.stdout.closed)
