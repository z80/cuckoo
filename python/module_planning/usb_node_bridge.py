import uasyncio
import ujson
import utime

from usb_node_protocol import (
    FrameParser, encode_frame, MAX_PAYLOAD,
    GET_NODE_ID, GET_NODES_QTY, GET_NODE_INFO,
    SEND_COMMAND, SEND_COMMAND_WAIT, OPEN_PIPE, SEND_PIPE,
    RESULT, ERROR, BUSY,
    ON_COMMAND, ON_PIPE_OPENED, ON_PIPE_DATA, ON_PIPE_CLOSED,
    CALLBACK_RESULT,
)


_WAITING = object()
_USB_READ_SIZE = 64
_USB_WRITE_TIMEOUT_MS = 500
_CALLBACK_TIMEOUT_MS = 1500
_UNASSIGNED = 0xFF


class USBNodeBridge:
    def __init__(self, node, usb):
        self.node = node
        self.usb = usb
        self._parser = FrameParser()
        self._rx = bytearray(_USB_READ_SIZE)
        self._write_lock = uasyncio.Lock()
        self._request_active = False
        self._event_id = 0
        self._callback_id = 0
        self._callback_value = _WAITING

        # The transport calls these methods from its RF processing task.
        node.on_command = self.on_command
        node.on_pipe_opened = self.on_pipe_opened
        node.on_pipe_data = self.on_pipe_data
        node.on_pipe_closed = self.on_pipe_closed

    def _next_event_id(self):
        self._event_id = (self._event_id % 255) + 1
        return self._event_id

    async def _write_frame(self, frame_type, request_id, payload=b""):
        frame = encode_frame(frame_type, request_id, payload)
        view = memoryview(frame)
        offset = 0
        started = utime.ticks_ms()

        await self._write_lock.acquire()
        try:
            while offset < len(frame):
                try:
                    written = self.usb.write(view[offset:])
                except OSError:
                    written = None
                if written:
                    offset += written
                    continue
                if utime.ticks_diff(utime.ticks_ms(), started) >= \
                        _USB_WRITE_TIMEOUT_MS:
                    return False
                await uasyncio.sleep_ms(1)
            return True
        finally:
            self._write_lock.release()

    async def _send_error(self, request_id, error):
        message = str(error).encode()
        if len(message) > 96:
            message = message[:96]
        await self._write_frame(ERROR, request_id, message)

    async def _execute_request(self, frame_type, request_id, payload):
        try:
            result = await self._dispatch_request(frame_type, payload)
            await self._write_frame(RESULT, request_id, result)
        except Exception as error:
            await self._send_error(request_id, error)
        finally:
            self._request_active = False

    async def _dispatch_request(self, frame_type, payload):
        if frame_type == GET_NODE_ID:
            node_id = self.node.node_id
            return bytes((
                node_id if node_id is not None else _UNASSIGNED,
            ))

        if frame_type == GET_NODES_QTY:
            quantity = await self.node.get_nodes_qty()
            return bytes((quantity,))

        if frame_type == GET_NODE_INFO:
            if len(payload) != 1:
                raise ValueError("bad node-info request")
            info = await self.node.get_node_info(payload[0])
            return b"" if info is None else ujson.dumps(info).encode()

        if frame_type == SEND_COMMAND or frame_type == SEND_COMMAND_WAIT:
            if len(payload) < 2:
                raise ValueError("bad command request")
            destination = payload[0]
            if frame_type == SEND_COMMAND:
                command = ujson.loads(payload[1:])
                await self.node.send_command(destination, command)
                return b""

            if len(payload) < 4:
                raise ValueError("bad command-wait request")
            timeout_ms = payload[1] | (payload[2] << 8)
            command = ujson.loads(payload[3:])
            reply = await self.node.send_command_and_wait_reply(
                destination, command, timeout_ms=timeout_ms
            )
            return ujson.dumps(reply).encode()

        if frame_type == OPEN_PIPE:
            if len(payload) != 1:
                raise ValueError("bad open-pipe request")
            pipe_id = await self.node.open_pipe(payload[0])
            return bytes((pipe_id,))

        if frame_type == SEND_PIPE:
            if len(payload) < 2:
                raise ValueError("bad pipe-data request")
            await self.node.send_pipe(
                payload[0], payload[2:], close=bool(payload[1])
            )
            return b""

        raise ValueError("unknown USB request")

    async def _handle_frame(self, frame_type, request_id, payload):
        if frame_type == CALLBACK_RESULT:
            if request_id != self._callback_id or \
                    self._callback_value is not _WAITING:
                return
            if not payload or payload[0] == 0:
                self._callback_value = None
            else:
                try:
                    self._callback_value = ujson.loads(payload[1:])
                except (ValueError, TypeError):
                    self._callback_value = None
            return

        if frame_type < GET_NODE_ID or frame_type > SEND_PIPE:
            return
        if self._request_active:
            await self._write_frame(BUSY, request_id)
            return

        self._request_active = True
        uasyncio.create_task(
            self._execute_request(frame_type, request_id, payload)
        )

    async def process(self):
        while True:
            try:
                count = self.usb.readinto(self._rx)
            except OSError:
                count = None

            if not count:
                await uasyncio.sleep_ms(1)
                continue

            for index in range(count):
                frame = self._parser.push(self._rx[index])
                if frame is not None:
                    await self._handle_frame(
                        frame[0], frame[1], frame[2]
                    )

    async def on_command(self, src_id, command):
        if self._callback_id:
            return None

        event_id = self._next_event_id()
        payload = bytes((src_id,)) + ujson.dumps(command).encode()
        self._callback_id = event_id
        self._callback_value = _WAITING
        if not await self._write_frame(ON_COMMAND, event_id, payload):
            self._callback_id = 0
            return None

        started = utime.ticks_ms()
        while self._callback_value is _WAITING:
            if utime.ticks_diff(utime.ticks_ms(), started) >= \
                    _CALLBACK_TIMEOUT_MS:
                self._callback_id = 0
                return None
            await uasyncio.sleep_ms(2)

        result = self._callback_value
        self._callback_value = _WAITING
        self._callback_id = 0
        return result

    async def on_pipe_opened(self, pipe_id, src_id):
        await self._write_frame(
            ON_PIPE_OPENED, self._next_event_id(),
            bytes((pipe_id, src_id)),
        )

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        max_data = MAX_PAYLOAD - 2
        offset = 0
        while offset < len(data_chunk):
            chunk = data_chunk[offset:offset + max_data]
            await self._write_frame(
                ON_PIPE_DATA, self._next_event_id(),
                bytes((pipe_id, src_id)) + chunk,
            )
            offset += len(chunk)

    async def on_pipe_closed(self, pipe_id, src_id):
        await self._write_frame(
            ON_PIPE_CLOSED, self._next_event_id(),
            bytes((pipe_id, src_id)),
        )
