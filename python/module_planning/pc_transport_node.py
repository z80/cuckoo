import json
import time

try:
    from serial import Serial, SerialException
except ImportError:
    Serial = None

    class SerialException(Exception):
        pass

from usb_node_protocol import (
    FrameParser, encode_frame, MAX_PAYLOAD,
    GET_NODE_ID, GET_NODES_QTY, GET_NODE_INFO,
    SEND_COMMAND, SEND_COMMAND_WAIT, OPEN_PIPE, SEND_PIPE,
    RESULT, ERROR, BUSY,
    ON_COMMAND, ON_PIPE_OPENED, ON_PIPE_DATA, ON_PIPE_CLOSED,
    ON_PIPE_FAILED,
    CALLBACK_RESULT,
)


DEFAULT_TIMEOUT = 3.0
MAX_DEFERRED_CALLS = 8
_UNASSIGNED = 0xFF


class TransportProxyError(Exception):
    pass


class TransportProxyTimeout(TransportProxyError):
    pass


class TransportProxyRemoteError(TransportProxyError):
    pass


class PCTransportNode:
    def __init__(self, port=None, baudrate=115200, timeout=DEFAULT_TIMEOUT,
                 serial_port=None):
        self._serial = serial_port if serial_port is not None else \
            self._open_serial(port, baudrate, timeout)
        self.timeout = timeout
        self.node_id = None
        self._parser = FrameParser()
        self._frames = []
        self._request_id = 0
        self._api_active = False
        self._inside_callback = False
        self._draining = False
        self._deferred = []
        self._open_pipes = set()

    @staticmethod
    def _open_serial(port, baudrate, timeout):
        if Serial is None:
            raise ImportError("pyserial is required for PC USB access")
        serial_port = Serial(
            port, baudrate, timeout=0, write_timeout=timeout
        )
        serial_port.reset_input_buffer()
        return serial_port

    def close(self):
        for pipe_id in tuple(self._open_pipes):
            try:
                self.send_pipe(pipe_id, b"", close=True)
            except Exception:
                pass
        self._open_pipes.clear()
        self._serial.close()

    def _next_request_id(self):
        self._request_id = (self._request_id % 255) + 1
        return self._request_id

    def _write_frame(self, frame_type, request_id, payload=b""):
        frame = encode_frame(frame_type, request_id, payload)
        view = memoryview(frame)
        offset = 0
        deadline = time.monotonic() + self.timeout
        while offset < len(frame):
            try:
                written = self._serial.write(view[offset:])
            except SerialException as error:
                raise TransportProxyError(str(error))
            if written:
                offset += written
                continue
            if time.monotonic() >= deadline:
                raise TransportProxyTimeout("USB write timed out")
            time.sleep(0.001)

    def _read_available(self):
        try:
            waiting = self._serial.in_waiting
            if not waiting:
                return
            chunk = self._serial.read(min(waiting, 64))
        except SerialException as error:
            raise TransportProxyError(str(error))

        for value in chunk:
            frame = self._parser.push(value)
            if frame is not None:
                self._frames.append(frame)

    def _read_frame(self, deadline):
        while True:
            if self._frames:
                return self._frames.pop(0)
            self._read_available()
            if self._frames:
                return self._frames.pop(0)
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.001)

    def _dispatch_event(self, frame_type, event_id, payload):
        if frame_type == ON_COMMAND:
            if len(payload) < 2:
                return
            try:
                command = json.loads(payload[1:])
            except (ValueError, TypeError):
                return

            result = None
            self._inside_callback = True
            try:
                result = self.on_command(payload[0], command)
            except Exception as error:
                self.on_callback_error(error)
            finally:
                self._inside_callback = False

            response = b"\x00"
            if result is not None:
                try:
                    response = b"\x01" + self._json(result)
                except Exception as error:
                    self.on_callback_error(error)
            self._write_frame(CALLBACK_RESULT, event_id, response)
            return

        try:
            self._inside_callback = True
            if frame_type == ON_PIPE_OPENED and len(payload) == 2:
                self.on_pipe_opened(payload[0], payload[1])
            elif frame_type == ON_PIPE_DATA and len(payload) >= 2:
                self.on_pipe_data(payload[0], payload[1], payload[2:])
            elif frame_type == ON_PIPE_CLOSED and len(payload) == 2:
                self.on_pipe_closed(payload[0], payload[1])
            elif frame_type == ON_PIPE_FAILED and len(payload) == 10:
                self.on_pipe_failed(
                    payload[0], payload[1],
                    int.from_bytes(payload[2:6], "little"),
                    int.from_bytes(payload[6:10], "little"),
                )
        except Exception as error:
            self.on_callback_error(error)
        finally:
            self._inside_callback = False

    def _call(self, frame_type, payload=b"", timeout=None):
        if self._inside_callback:
            raise RuntimeError(
                "use defer() for API calls made by callbacks"
            )
        if self._api_active:
            raise RuntimeError("another API call is already active")

        self._api_active = True
        request_id = self._next_request_id()
        deadline = time.monotonic() + (
            self.timeout if timeout is None else timeout
        )
        try:
            self._write_frame(frame_type, request_id, payload)
            while True:
                frame = self._read_frame(deadline)
                if frame is None:
                    raise TransportProxyTimeout("USB request timed out")
                response_type, response_id, response_payload = frame

                if ON_COMMAND <= response_type <= ON_PIPE_CLOSED or \
                        response_type == ON_PIPE_FAILED:
                    self._dispatch_event(
                        response_type, response_id, response_payload
                    )
                    continue

                if response_id != request_id:
                    continue
                if response_type == RESULT:
                    return response_payload
                if response_type == BUSY:
                    raise TransportProxyRemoteError("MCU is busy")
                if response_type == ERROR:
                    raise TransportProxyRemoteError(
                        response_payload.decode("utf-8", "replace")
                    )
        finally:
            self._api_active = False
            if not self._inside_callback and not self._draining:
                self._drain_deferred()

    @staticmethod
    def _json(value):
        return json.dumps(
            value, separators=(",", ":"), ensure_ascii=True
        ).encode()

    def defer(self, method, *args, on_result=None, on_error=None, **kwargs):
        if len(self._deferred) >= MAX_DEFERRED_CALLS:
            raise RuntimeError("deferred call queue full")
        self._deferred.append(
            (method, args, kwargs, on_result, on_error)
        )

    def _drain_deferred(self):
        if self._draining or self._api_active or self._inside_callback:
            return
        self._draining = True
        try:
            while self._deferred:
                method, args, kwargs, on_result, on_error = \
                    self._deferred.pop(0)
                try:
                    result = method(*args, **kwargs)
                    if on_result is not None:
                        on_result(result)
                except Exception as error:
                    if on_error is not None:
                        on_error(error)
                    else:
                        self.on_deferred_error(error)
        finally:
            self._draining = False

    def poll(self, timeout_ms=0):
        deadline = time.monotonic() + timeout_ms / 1000.0
        processed = 0
        while True:
            frame = self._read_frame(deadline)
            if frame is None:
                break
            frame_type, event_id, payload = frame
            if ON_COMMAND <= frame_type <= ON_PIPE_CLOSED or \
                    frame_type == ON_PIPE_FAILED:
                self._dispatch_event(frame_type, event_id, payload)
                processed += 1
            if timeout_ms == 0:
                break
        self._drain_deferred()
        return processed

    def process(self, poll_ms=50):
        while True:
            self.poll(poll_ms)

    def get_node_id(self):
        payload = self._call(GET_NODE_ID)
        if len(payload) != 1:
            raise TransportProxyError("invalid node-id response")
        self.node_id = None if payload[0] == _UNASSIGNED else payload[0]
        return self.node_id

    def get_nodes_qty(self):
        payload = self._call(GET_NODES_QTY)
        if len(payload) != 1:
            raise TransportProxyError("invalid node-count response")
        return payload[0]

    def get_node_info(self, node_index):
        if not 0 <= node_index <= 255:
            raise ValueError("invalid node index")
        payload = self._call(GET_NODE_INFO, bytes((node_index,)))
        return None if not payload else json.loads(payload)

    def send_command(self, node_id, command):
        self._call(
            SEND_COMMAND, bytes((node_id,)) + self._json(command)
        )
        return True

    def send_command_and_wait_reply(self, node_id, command,
                                    timeout_ms=2000):
        if not 1 <= timeout_ms <= 0xFFFF:
            raise ValueError("invalid request timeout")
        payload = bytes((
            node_id,
            timeout_ms & 0xFF,
            timeout_ms >> 8,
        )) + self._json(command)
        response = self._call(
            SEND_COMMAND_WAIT, payload,
            timeout=max(self.timeout, timeout_ms / 1000.0 + 1.0),
        )
        return json.loads(response)

    def open_pipe(self, node_id):
        payload = self._call(OPEN_PIPE, bytes((node_id,)))
        if len(payload) != 1:
            raise TransportProxyError("invalid pipe response")
        pipe_id = payload[0]
        self._open_pipes.add(pipe_id)
        return pipe_id

    def send_pipe(self, pipe_id, data, close=False):
        data = bytes(data)
        max_chunk = MAX_PAYLOAD - 2
        if not data:
            self._call(
                SEND_PIPE, bytes((pipe_id, 1 if close else 0))
            )
            if close:
                self._open_pipes.discard(pipe_id)
            return

        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + max_chunk]
            offset += len(chunk)
            final = close and offset >= len(data)
            self._call(
                SEND_PIPE,
                bytes((pipe_id, 1 if final else 0)) + chunk,
            )
        if close:
            self._open_pipes.discard(pipe_id)

    # Override these callbacks in the PC application.
    def on_command(self, src_id, command):
        return None

    def on_pipe_opened(self, pipe_id, src_id):
        pass

    def on_pipe_data(self, pipe_id, src_id, data_chunk):
        pass

    def on_pipe_closed(self, pipe_id, src_id):
        pass

    def on_pipe_failed(self, pipe_id, src_id, reason, transferred_bytes):
        pass

    def on_callback_error(self, error):
        pass

    def on_deferred_error(self, error):
        pass
