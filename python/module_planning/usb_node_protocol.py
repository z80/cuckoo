try:
    from micropython import const
except ImportError:
    const = lambda value: value


MAGIC_0 = const(0xA5)
MAGIC_1 = const(0x5A)
HEADER_SIZE = const(6)
MAX_PAYLOAD = const(256)
MAX_FRAME_SIZE = const(HEADER_SIZE + MAX_PAYLOAD)

# PC -> MCU API requests.
GET_NODE_ID = const(1)
GET_NODES_QTY = const(2)
GET_NODE_INFO = const(3)
SEND_COMMAND = const(4)
SEND_COMMAND_WAIT = const(5)
OPEN_PIPE = const(6)
SEND_PIPE = const(7)

# MCU -> PC API responses.
RESULT = const(0x20)
ERROR = const(0x21)
BUSY = const(0x22)

# MCU -> PC callbacks and the callback response.
ON_COMMAND = const(0x40)
ON_PIPE_OPENED = const(0x41)
ON_PIPE_DATA = const(0x42)
ON_PIPE_CLOSED = const(0x43)
CALLBACK_RESULT = const(0x44)

def encode_frame(frame_type, request_id, payload=b""):
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        payload = bytes(payload)
    length = len(payload)
    if length > MAX_PAYLOAD:
        raise ValueError("USB payload too large")

    frame = bytearray(HEADER_SIZE + length)
    frame[0] = MAGIC_0
    frame[1] = MAGIC_1
    frame[2] = frame_type
    frame[3] = request_id
    frame[4] = length & 0xFF
    frame[5] = length >> 8
    frame[HEADER_SIZE:HEADER_SIZE + length] = payload
    return frame


class FrameParser:
    def __init__(self):
        self._buffer = bytearray(MAX_FRAME_SIZE)
        self._used = 0
        self._expected = 0

    def reset(self):
        self._used = 0
        self._expected = 0

    def _restart(self, value):
        self._expected = 0
        if value == MAGIC_0:
            self._buffer[0] = value
            self._used = 1
        else:
            self._used = 0

    def push(self, value):
        if self._used == 0:
            if value == MAGIC_0:
                self._buffer[0] = value
                self._used = 1
            return None

        if self._used == 1:
            if value != MAGIC_1:
                self._restart(value)
                return None
            self._buffer[1] = value
            self._used = 2
            return None

        self._buffer[self._used] = value
        self._used += 1

        if self._used == HEADER_SIZE:
            length = self._buffer[4] | (self._buffer[5] << 8)
            if length > MAX_PAYLOAD:
                self._restart(value)
                return None
            self._expected = HEADER_SIZE + length

        if not self._expected or self._used < self._expected:
            return None

        frame_type = self._buffer[2]
        request_id = self._buffer[3]
        payload = bytes(self._buffer[HEADER_SIZE:self._expected])
        self.reset()
        return frame_type, request_id, payload
