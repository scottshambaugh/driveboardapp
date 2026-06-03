"""Shared test helpers."""


class FakeSerialDevice:
    """Minimal stand-in for a pyserial device used in driveboard tests.

    ``read`` drains a preloaded byte queue (for exercising the RX parser);
    ``write`` records everything sent (for asserting TX-side output).
    """

    def __init__(self, rx=b""):
        self._rx = bytearray(rx)
        self.written = bytearray()
        self.closed = False

    def read(self, n):
        chunk = bytes(self._rx[:n])
        del self._rx[:n]
        return chunk

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def flushOutput(self):
        pass

    def flushInput(self):
        pass

    def close(self):
        self.closed = True
