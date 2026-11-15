"""Read the latest raw frame from Android's screenrecord stream."""

from contextlib import suppress
import struct
import subprocess
from threading import Condition, Thread
from time import monotonic

from PIL import Image


_HEADER = struct.Struct("<IIIII")
_RGB_BYTES = 3
_MAX_FRAME_BYTES = 64 * 1024 * 1024


def _read_frame(pipe, expected_size):
    # The subprocess pipe is buffered, so read(n) only returns less at EOF.
    header = pipe.read(_HEADER.size)
    if len(header) != _HEADER.size:
        return None
    packet_size, width, height, stride, bytes_per_pixel = _HEADER.unpack(header)
    pixel_bytes = stride * height
    if (
        (width, height) != expected_size
        or bytes_per_pixel != _RGB_BYTES
        or stride < width * _RGB_BYTES
        or not 0 < pixel_bytes <= _MAX_FRAME_BYTES
        or packet_size != _HEADER.size - 4 + pixel_bytes
    ):
        return None
    received_at = monotonic()
    pixels = pipe.read(pixel_bytes)
    if len(pixels) != pixel_bytes:
        return None
    return received_at, Image.frombytes("RGB", expected_size, pixels, "raw", "RGB", stride, 1)


class BattleFrameStream:
    """Keep at most one fresh, native-size RGB frame from the selected emulator."""

    def __init__(self, adb, serial, size):
        width, height = size
        if width <= 0 or height <= 0 or width * height * _RGB_BYTES > _MAX_FRAME_BYTES:
            raise ValueError(f"Invalid screenrecord size: {width}x{height}")
        self._condition = Condition()
        self._frame = None
        self._finished = False
        self._closed = False
        self._process = subprocess.Popen(
            [str(adb), "-s", serial, "exec-out", "screenrecord", "--size", f"{width}x{height}",
             "--output-format=frames", "--time-limit", "180", "-"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = Thread(target=self._collect, args=(size,), daemon=True)
        self._reader.start()

    def _collect(self, size):
        try:
            with self._process.stdout as pipe:
                while (frame := _read_frame(pipe, size)) is not None:
                    with self._condition:
                        if self._closed:
                            break
                        self._frame = frame
                        self._condition.notify()
        except (OSError, ValueError):
            # An interrupted pipe or unsupported format leaves ADB capture available.
            pass
        finally:
            with self._condition:
                self._finished = True
                self._condition.notify_all()

    def next_frame(self, timeout):
        """Wait up to timeout seconds for a frame that has not been returned before."""
        with self._condition:
            self._condition.wait_for(lambda: self._frame is not None or self._finished, timeout)
            frame, self._frame = self._frame, None
            return frame

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = self._finished = True
            self._frame = None
            self._condition.notify_all()
        if self._process.poll() is None:
            with suppress(ProcessLookupError):
                self._process.kill()
        with suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=1)
        # The reader owns stdout so it can close the pipe after a pending read.
        self._reader.join(timeout=1)
