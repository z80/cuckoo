"""Remote facade matching the high-level PC hardware-node API."""

import asyncio
from collections import deque

from pc_hardware_node import (
    HardwareNodeError,
    MIC_CLOSE_TIMEOUT,
    MIC_QUEUE_BYTES,
    MicStreamError,
    SAMPLE_RATE,
)
from pc_transport_node_async import (
    DEFAULT_TIMEOUT,
    TransportProxyError,
    TransportProxyRemoteError,
    TransportProxyTimeout,
)

from .connection import (
    RelayBusyError,
    RelayClientConnection,
    RelayConnectionError,
    RelayTimeoutError,
    RemoteRelayError,
)
from .protocol import EVENT, HARDWARE


MAX_PLAY_SECONDS = 30
MAX_PLAY_BYTES = MAX_PLAY_SECONDS * SAMPLE_RATE * 2


class RemoteMicStream:
    """Loss-intolerant microphone stream forwarded by a relay server."""

    def __init__(self, source_id, limit=MIC_QUEUE_BYTES):
        self.source_id = source_id
        self.stream_id = None
        self.pipe_id = None
        self.overrun_count = 0
        self.dropped_bytes = 0
        self._limit = limit
        self._chunks = deque()
        self._queued_bytes = 0
        self._changed = asyncio.Event()
        self._closed = False
        self._failure = None

    @property
    def queued_bytes(self):
        return self._queued_bytes

    @property
    def is_open(self):
        return self.stream_id is not None and not self._closed

    @property
    def is_closed(self):
        return self._closed

    async def wait_closed(self, timeout=None):
        async def wait():
            while not self._closed:
                self._changed.clear()
                if not self._closed:
                    await self._changed.wait()

        if timeout is None:
            await wait()
        else:
            await asyncio.wait_for(wait(), timeout)
        if self._failure is not None:
            raise self._failure

    async def read(self, timeout_ms=None):
        async def wait_for_data():
            while not self._chunks and not self._closed:
                self._changed.clear()
                if not self._chunks and not self._closed:
                    await self._changed.wait()

        if timeout_ms is None:
            await wait_for_data()
        else:
            if timeout_ms < 0:
                raise ValueError("timeout_ms must be non-negative")
            await asyncio.wait_for(wait_for_data(), timeout_ms / 1000.0)
        if self._chunks:
            if len(self._chunks) == 1:
                data = self._chunks.popleft()
            else:
                data = b"".join(self._chunks)
                self._chunks.clear()
            self._queued_bytes = 0
            return data
        if self._failure is not None:
            raise self._failure
        return b""

    def __aiter__(self):
        return self

    async def __anext__(self):
        data = await self.read()
        if not data:
            raise StopAsyncIteration
        return data

    def _bind(self, stream_id, pipe_id=None):
        if self.stream_id is not None and self.stream_id != stream_id:
            return False
        self.stream_id = stream_id
        self.pipe_id = pipe_id
        return True

    def _feed(self, data, overrun_count=0, dropped_bytes=0):
        if self._closed or not data:
            return True
        self.overrun_count = overrun_count
        self.dropped_bytes = dropped_bytes
        if overrun_count or dropped_bytes:
            self._finish(MicStreamError(
                "microphone relay detected data loss: overruns={}, "
                "dropped={}".format(overrun_count, dropped_bytes)
            ))
            return False
        data = bytes(data)
        if self._queued_bytes + len(data) > self._limit:
            self._finish(MicStreamError(
                "microphone relay consumer is too slow"
            ))
            return False
        self._chunks.append(data)
        self._queued_bytes += len(data)
        self._changed.set()
        return True

    def _finish(self, failure=None):
        if self._closed:
            return
        self._closed = True
        self._failure = failure
        self._changed.set()


class RemoteHardwareNode:
    """Network-backed equivalent of the public ``PCHardwareNode`` API."""

    def __init__(self, connection, timeout=DEFAULT_TIMEOUT):
        self._connection = connection
        self.timeout = timeout
        self.node_id = None
        self._mic_stream = None
        self._event_queue = asyncio.Queue()
        self._event_task = asyncio.create_task(self._event_loop())
        connection.set_message_handler(self._receive_message)
        connection.set_disconnect_handler(self._disconnected)

    @classmethod
    async def connect(cls, endpoint, timeout=DEFAULT_TIMEOUT, **kwargs):
        connection = await RelayClientConnection.connect(
            endpoint, HARDWARE, **kwargs
        )
        return cls(connection, timeout=timeout)

    async def close(self):
        await self._connection.close()
        stream = self._mic_stream
        if stream is not None:
            stream._finish(MicStreamError("hardware relay closed"))
            self._mic_stream = None
        self._event_task.cancel()
        await asyncio.gather(self._event_task, return_exceptions=True)

    async def get_node_id(self):
        value = (await self._call("get_node_id")).get("value")
        if value is not None and not isinstance(value, int):
            raise TransportProxyError("invalid node-id response")
        self.node_id = value
        return value

    async def get_nodes_qty(self):
        value = (await self._call("get_nodes_qty")).get("value")
        if not isinstance(value, int):
            raise TransportProxyError("invalid node-count response")
        return value

    async def get_node_info(self, node_index):
        if not 0 <= node_index <= 255:
            raise ValueError("invalid node index")
        return (await self._call(
            "get_node_info", {"node_index": node_index}
        )).get("value")

    async def get_core_diagnostics(self):
        value = (await self._call("get_core_diagnostics")).get("value")
        if not isinstance(value, dict):
            raise TransportProxyError("invalid core diagnostics response")
        if isinstance(value.get("stats"), list):
            value["stats"] = tuple(value["stats"])
        return value

    async def play_buffer(self, node_id, data):
        data = bytes(data)
        if not data or len(data) & 1:
            raise ValueError("speaker data must contain whole 16-bit samples")
        if len(data) > MAX_PLAY_BYTES:
            raise ValueError("speaker data exceeds 30-second limit")
        timeout = max(30.0, len(data) / 4000.0 + 30.0)
        await self._call(
            "play_buffer",
            {"node_id": node_id},
            data=data,
            timeout=timeout,
            bulk=True,
        )

    async def start_mic_stream(self, node_id):
        if self._mic_stream is not None and not self._mic_stream.is_closed:
            raise HardwareNodeError("microphone stream already active")
        stream = RemoteMicStream(node_id)
        self._mic_stream = stream
        try:
            reply = await self._call(
                "start_mic_stream", {"node_id": node_id}, timeout=15.0
            )
            stream_id = reply.get("stream_id")
            if not isinstance(stream_id, int):
                raise HardwareNodeError("invalid microphone stream response")
            if not stream._bind(stream_id, reply.get("pipe_id")):
                raise HardwareNodeError("microphone stream changed")
            return stream
        except BaseException as error:
            stream._finish(
                error if isinstance(error, Exception)
                else MicStreamError("microphone start cancelled")
            )
            if self._mic_stream is stream:
                self._mic_stream = None
            raise

    async def stop_mic_stream(self, node_id):
        stream = self._mic_stream
        if stream is None or stream.source_id != node_id:
            raise HardwareNodeError("no microphone stream for node")
        await self._call(
            "stop_mic_stream", {"node_id": node_id}, timeout=15.0
        )
        try:
            await stream.wait_closed(MIC_CLOSE_TIMEOUT)
        finally:
            if self._mic_stream is stream and stream.is_closed:
                self._mic_stream = None

    async def set_pyro_enable(self, node_id, en):
        return bool((await self._call(
            "set_pyro_enable", {"node_id": node_id, "enable": bool(en)}
        )).get("value"))

    async def get_pyro_state(self, node_id):
        return bool((await self._call(
            "get_pyro_state", {"node_id": node_id}
        )).get("value"))

    async def _call(
        self, operation, metadata=None, data=None, timeout=None, bulk=False
    ):
        try:
            message = await self._connection.call(
                operation,
                metadata,
                data,
                timeout=self.timeout + 2.0 if timeout is None else timeout,
                bulk=bulk,
            )
            return dict(message.metadata or {})
        except RelayTimeoutError as error:
            raise TransportProxyTimeout(str(error)) from error
        except RemoteRelayError as error:
            if error.error_type in ("HardwareNodeError", "MicStreamError"):
                raise HardwareNodeError(str(error)) from error
            raise TransportProxyRemoteError(str(error)) from error
        except RelayBusyError as error:
            raise TransportProxyRemoteError(str(error)) from error
        except RelayConnectionError as error:
            raise TransportProxyError(str(error)) from error

    async def _receive_message(self, message):
        if message.kind == EVENT:
            await self._event_queue.put(message)

    async def _event_loop(self):
        try:
            while True:
                message = await self._event_queue.get()
                try:
                    accepted = self._handle_event(message)
                    if accepted is False:
                        stream = self._mic_stream
                        if stream is not None:
                            try:
                                await self._call(
                                    "stop_mic_stream",
                                    {"node_id": stream.source_id},
                                    timeout=15.0,
                                )
                            except Exception:
                                pass
                except Exception as error:
                    stream = self._mic_stream
                    if stream is not None:
                        stream._finish(error)
                finally:
                    self._event_queue.task_done()
        except asyncio.CancelledError:
            pass

    def _handle_event(self, message):
        metadata = message.metadata or {}
        operation = metadata.get("operation")
        stream = self._mic_stream
        stream_id = metadata.get("stream_id")
        if stream is None:
            return
        if stream.stream_id is None:
            stream._bind(stream_id, metadata.get("pipe_id"))
        if stream_id != stream.stream_id:
            return
        if operation == "mic_data":
            return stream._feed(
                message.data or b"",
                metadata.get("overrun_count", 0),
                metadata.get("dropped_bytes", 0),
            )
        elif operation == "mic_closed":
            stream._finish()
        elif operation == "mic_error":
            stream._finish(MicStreamError(
                metadata.get("message", "microphone relay failed")
            ))
        return True

    async def _disconnected(self, error):
        stream = self._mic_stream
        if stream is not None:
            stream._finish(MicStreamError(str(error)))


__all__ = (
    "MAX_PLAY_BYTES",
    "MAX_PLAY_SECONDS",
    "RemoteHardwareNode",
    "RemoteMicStream",
)
