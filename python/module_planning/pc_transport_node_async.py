import asyncio
import json
from typing import Dict, Optional, Any

try:
    import serial_asyncio
except ImportError:
    serial_asyncio = None

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
_UNASSIGNED = 0xFF


class TransportProxyError(Exception):
    pass


class TransportProxyTimeout(TransportProxyError):
    pass


class TransportProxyRemoteError(TransportProxyError):
    pass


class AsyncPCTransportNode:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, timeout: float = DEFAULT_TIMEOUT):
        self.reader = reader
        self.writer = writer
        self.timeout = timeout
        self.node_id: Optional[int] = None

        self._parser = FrameParser()
        self._request_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._open_pipes = set()

        # Keep callbacks ordered independently of request/response delivery.
        self._event_queue = asyncio.Queue()
        self._event_task = asyncio.create_task(self._event_loop())
        self._reader_task = asyncio.create_task(self._listen_loop())

    @classmethod
    async def create(cls, port: str, baudrate: int = 115200, timeout: float = DEFAULT_TIMEOUT):
        """Factory method to open serial connection asynchronously."""
        if serial_asyncio is None:
            raise ImportError("pyserial-asyncio is required for async PC USB access")
            
        reader, writer = await serial_asyncio.open_serial_connection(
            url=port, baudrate=baudrate
        )
        transport = getattr(writer, "transport", None)
        serial_port = getattr(transport, "serial", None)
        if serial_port is not None and \
                hasattr(serial_port, "reset_input_buffer"):
            serial_port.reset_input_buffer()
        return cls(reader, writer, timeout=timeout)

    async def close(self):
        """Gracefully close the transport and cancel background tasks."""
        for pipe_id in tuple(self._open_pipes):
            try:
                await self.send_pipe(pipe_id, b"", close=True)
            except Exception:
                pass
        self._open_pipes.clear()

        self._reader_task.cancel()
        try:
            await self._reader_task
        except asyncio.CancelledError:
            pass

        self._event_task.cancel()
        try:
            await self._event_task
        except asyncio.CancelledError:
            pass

        self.writer.close()
        await self.writer.wait_closed()

        # Fail any remaining pending futures
        for fut in self._pending_requests.values():
            if not fut.done():
                fut.set_exception(TransportProxyError("Transport closed"))
        self._pending_requests.clear()

    def _next_request_id(self) -> int:
        self._request_id = (self._request_id % 255) + 1
        return self._request_id

    async def _write_frame(self, frame_type: int, request_id: int, payload: bytes = b""):
        frame = encode_frame(frame_type, request_id, payload)
        async with self._write_lock:
            self.writer.write(frame)
            await self.writer.drain()

    async def _listen_loop(self):
        """Background coroutine to read incoming stream and dispatch frames."""
        try:
            while True:
                chunk = await self.reader.read(64)
                if not chunk:
                    await asyncio.sleep(0.001)
                    continue

                for byte in chunk:
                    frame = self._parser.push(byte)
                    if frame is not None:
                        frame_type, resp_id, payload = frame

                        if ON_COMMAND <= frame_type <= ON_PIPE_CLOSED or \
                                frame_type == ON_PIPE_FAILED:
                            self._event_queue.put_nowait(
                                (frame_type, resp_id, payload)
                            )
                        else:
                            # Deliver response to the matching waiting request
                            fut = self._pending_requests.pop(resp_id, None)
                            if fut and not fut.done():
                                fut.set_result((frame_type, payload))
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.on_callback_error(error)

    async def _event_loop(self):
        """Dispatch callbacks in exactly the order received over USB."""
        try:
            while True:
                frame_type, event_id, payload = await self._event_queue.get()
                try:
                    try:
                        await self._dispatch_event(
                            frame_type, event_id, payload
                        )
                    except Exception as error:
                        self.on_callback_error(error)
                finally:
                    self._event_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self.on_callback_error(error)

    async def _dispatch_event(self, frame_type: int, event_id: int, payload: bytes):
        if frame_type == ON_COMMAND:
            if len(payload) < 2:
                return
            try:
                command = json.loads(payload[1:])
            except (ValueError, TypeError):
                return

            result = None
            try:
                res = self.on_command(payload[0], command)
                result = await res if asyncio.iscoroutine(res) else res
            except Exception as error:
                self.on_callback_error(error)

            response = b"\x00"
            if result is not None:
                try:
                    response = b"\x01" + self._json(result)
                except Exception as error:
                    self.on_callback_error(error)
            
            await self._write_frame(CALLBACK_RESULT, event_id, response)
            return

        try:
            if frame_type == ON_PIPE_OPENED and len(payload) == 2:
                res = self.on_pipe_opened(payload[0], payload[1])
                if asyncio.iscoroutine(res): await res
            elif frame_type == ON_PIPE_DATA and len(payload) >= 2:
                res = self.on_pipe_data(payload[0], payload[1], payload[2:])
                if asyncio.iscoroutine(res): await res
            elif frame_type == ON_PIPE_CLOSED and len(payload) == 2:
                res = self.on_pipe_closed(payload[0], payload[1])
                if asyncio.iscoroutine(res): await res
            elif frame_type == ON_PIPE_FAILED and len(payload) == 10:
                reason = int.from_bytes(payload[2:6], "little")
                transferred = int.from_bytes(payload[6:10], "little")
                res = self.on_pipe_failed(
                    payload[0], payload[1], reason, transferred
                )
                if asyncio.iscoroutine(res): await res
        except Exception as error:
            self.on_callback_error(error)

    async def _call(self, frame_type: int, payload: bytes = b"", timeout: Optional[float] = None) -> bytes:
        request_id = self._next_request_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = fut

        call_timeout = self.timeout if timeout is None else timeout

        try:
            await self._write_frame(frame_type, request_id, payload)
            response_type, response_payload = await asyncio.wait_for(fut, timeout=call_timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(request_id, None)
            raise TransportProxyTimeout("USB request timed out")
        except Exception:
            self._pending_requests.pop(request_id, None)
            raise

        if response_type == RESULT:
            return response_payload
        if response_type == BUSY:
            raise TransportProxyRemoteError("MCU is busy")
        if response_type == ERROR:
            raise TransportProxyRemoteError(response_payload.decode("utf-8", "replace"))

        raise TransportProxyError(f"Unexpected response frame type: {response_type}")

    @staticmethod
    def _json(value: Any) -> bytes:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()

    # --- API Methods ---

    async def get_node_id(self) -> Optional[int]:
        payload = await self._call(GET_NODE_ID)
        if len(payload) != 1:
            raise TransportProxyError("invalid node-id response")
        self.node_id = None if payload[0] == _UNASSIGNED else payload[0]
        return self.node_id

    async def get_nodes_qty(self) -> int:
        payload = await self._call(GET_NODES_QTY)
        if len(payload) != 1:
            raise TransportProxyError("invalid node-count response")
        return payload[0]

    async def get_node_info(self, node_index: int) -> Optional[dict]:
        if not 0 <= node_index <= 255:
            raise ValueError("invalid node index")
        payload = await self._call(GET_NODE_INFO, bytes((node_index,)))
        return None if not payload else json.loads(payload)

    async def send_command(self, node_id: int, command: Any) -> bool:
        await self._call(SEND_COMMAND, bytes((node_id,)) + self._json(command))
        return True

    async def send_command_and_wait_reply(self, node_id: int, command: Any, timeout_ms: int = 2000) -> Any:
        if not 1 <= timeout_ms <= 0xFFFF:
            raise ValueError("invalid request timeout")
        payload = bytes((
            node_id,
            timeout_ms & 0xFF,
            timeout_ms >> 8,
        )) + self._json(command)
        
        response = await self._call(
            SEND_COMMAND_WAIT, payload,
            timeout=max(self.timeout, timeout_ms / 1000.0 + 1.0),
        )
        return json.loads(response)

    async def open_pipe(self, node_id: int) -> int:
        payload = await self._call(OPEN_PIPE, bytes((node_id,)))
        if len(payload) != 1:
            raise TransportProxyError("invalid pipe response")
        pipe_id = payload[0]
        self._open_pipes.add(pipe_id)
        return pipe_id

    async def send_pipe(self, pipe_id: int, data: bytes, close: bool = False):
        data = bytes(data)
        max_chunk = MAX_PAYLOAD - 2
        if not data:
            await self._call(SEND_PIPE, bytes((pipe_id, 1 if close else 0)))
            if close:
                self._open_pipes.discard(pipe_id)
            return

        offset = 0
        while offset < len(data):
            chunk = data[offset:offset + max_chunk]
            offset += len(chunk)
            final = close and offset >= len(data)
            await self._call(
                SEND_PIPE,
                bytes((pipe_id, 1 if final else 0)) + chunk,
            )
        if close:
            self._open_pipes.discard(pipe_id)

    # --- Callbacks (Override in PC App) ---
    async def on_command(self, src_id: int, command: Any) -> Any:
        return None

    async def on_pipe_opened(self, pipe_id: int, src_id: int):
        pass

    async def on_pipe_data(self, pipe_id: int, src_id: int, data_chunk: bytes):
        pass

    async def on_pipe_closed(self, pipe_id: int, src_id: int):
        pass

    async def on_pipe_failed(self, pipe_id: int, src_id: int,
                             reason: int, transferred_bytes: int):
        pass

    def on_callback_error(self, error: Exception):
        pass
