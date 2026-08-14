"""Async PC facade for the peripherals exposed by ``main_slave_dac_adc``.

The class deliberately has the same node-oriented shape as the transport
proxy: every operation names its remote node.  It adds only the peripheral
protocol and the callback routing needed by the speaker and microphone.
"""

import asyncio
from collections import deque

from pc_transport_node_async import AsyncPCTransportNode


MIC_QUEUE_BYTES = 64 * 1024
MIC_OPEN_TIMEOUT = 10.0
MIC_CLOSE_TIMEOUT = 10.0
SPEAKER_POLL_SECONDS = 0.05
SAMPLE_RATE = 16000


class HardwareNodeError(RuntimeError):
    pass


class MicStreamError(HardwareNodeError):
    pass


class MicStream:
    """Bounded asynchronous stream of microphone bytes.

    ``read()`` waits for data and then returns everything queued at that
    instant.  A normal remote close is represented by ``b\"\"`` after queued
    data has been drained.  Pipe failures are raised after queued data has
    been drained.  If the consumer falls behind, complete oldest chunks are
    discarded so acquisition can continue.
    """

    def __init__(self, source_id, limit=MIC_QUEUE_BYTES):
        self.source_id = source_id
        self.pipe_id = None
        self.overrun_count = 0
        self.dropped_bytes = 0

        self._limit = limit
        self._chunks = deque()
        self._queued_bytes = 0
        self._changed = asyncio.Event()
        self._opened = asyncio.Event()
        self._closed = False
        self._failure = None

    @property
    def queued_bytes(self):
        return self._queued_bytes

    @property
    def is_open(self):
        return self.pipe_id is not None and not self._closed

    @property
    def is_closed(self):
        return self._closed

    async def wait_open(self, timeout=MIC_OPEN_TIMEOUT):
        await asyncio.wait_for(self._opened.wait(), timeout)
        if self._failure is not None:
            raise self._failure
        return self

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
        """Return currently accumulated bytes, waiting for the first byte.

        ``timeout_ms=None`` waits indefinitely.  A finite timeout raises
        ``asyncio.TimeoutError`` in the usual way.
        """

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

    def _bind(self, pipe_id):
        if self.pipe_id is not None and self.pipe_id != pipe_id:
            return False
        self.pipe_id = pipe_id
        self._opened.set()
        return True

    def _feed(self, data):
        if self._closed or not data:
            return
        data = bytes(data)
        if len(data) > self._limit:
            dropped = len(data) - self._limit
            data = data[-self._limit:]
            self.overrun_count += 1
            self.dropped_bytes += dropped

        dropped = 0
        while self._chunks and self._queued_bytes + len(data) > self._limit:
            old = self._chunks.popleft()
            self._queued_bytes -= len(old)
            dropped += len(old)
        if dropped:
            self.overrun_count += 1
            self.dropped_bytes += dropped

        self._chunks.append(data)
        self._queued_bytes += len(data)
        self._changed.set()

    def _finish(self, failure=None):
        if self._closed:
            return
        self._closed = True
        self._failure = failure
        # Also release start_mic_stream if opening itself failed.
        self._opened.set()
        self._changed.set()


class _SpeakerTransfer:
    def __init__(self, node_id, data):
        self.node_id = node_id
        self.data = memoryview(data)
        self.offset = 0
        self.pipe_id = None
        self.all_sent = asyncio.Event()
        self.failure = None


class PCHardwareNode(AsyncPCTransportNode):
    """PC transport node with speaker, microphone and pyro operations."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._speaker = None
        self._mic_stream = None

    async def play_buffer(self, node_id, data):
        """Play unsigned, right-aligned 12-bit samples on a remote speaker.

        The samples are passed as little-endian 16-bit words.  This method
        returns only after the slave reports that its DAC has finished and
        speaker power has been turned off.
        """

        if self._speaker is not None:
            raise HardwareNodeError("speaker transfer already active")
        data = bytes(data)
        if not data or len(data) & 1:
            raise ValueError("speaker data must contain whole 16-bit samples")

        transfer = _SpeakerTransfer(node_id, data)
        self._speaker = transfer
        try:
            reply = await self.send_command_and_wait_reply(
                node_id,
                {"cmd": "speaker", "op": "play", "bytes": len(data),
                 "rate": SAMPLE_RATE},
                timeout_ms=5000,
            )
            self._require_ok(reply, "speaker play")

            transfer.pipe_id = await self.open_pipe(node_id)

            # A failed receiver or link should not hold this call forever.
            transfer_timeout = max(15.0, len(data) / 8000.0 + 10.0)
            await asyncio.wait_for(
                transfer.all_sent.wait(), transfer_timeout
            )
            if transfer.failure is not None:
                raise transfer.failure

            # Closing the pipe means all bytes reached the remote transport,
            # not that the last DMA/timed DAC buffer has completed.
            deadline = asyncio.get_running_loop().time() + transfer_timeout
            while True:
                reply = await self.send_command_and_wait_reply(
                    node_id,
                    {"cmd": "speaker", "op": "state"},
                    timeout_ms=3000,
                )
                self._require_ok(reply, "speaker state")
                error = reply.get("error")
                if error:
                    raise HardwareNodeError("speaker failed: {}".format(error))
                if reply.get("state") == "idle":
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError
                await asyncio.sleep(SPEAKER_POLL_SECONDS)
        except BaseException:
            await self._close_speaker_pipe(transfer)
            raise
        finally:
            if self._speaker is transfer:
                self._speaker = None

    async def start_mic_stream(self, node_id):
        """Start a remote microphone and return its asynchronous byte stream."""

        if self._mic_stream is not None and not self._mic_stream.is_closed:
            raise HardwareNodeError("microphone stream already active")

        stream = MicStream(node_id)
        # Install routing before sending the command: the incoming open event
        # may race with delivery of the command reply.
        self._mic_stream = stream
        try:
            reply = await self.send_command_and_wait_reply(
                node_id,
                {"cmd": "mic", "op": "start"},
                timeout_ms=5000,
            )
            self._require_ok(reply, "microphone start")
            return await stream.wait_open()
        except BaseException as error:
            stream._finish(
                error if isinstance(error, Exception)
                else MicStreamError("microphone start cancelled")
            )
            if self._mic_stream is stream:
                self._mic_stream = None
            raise

    async def stop_mic_stream(self, node_id):
        """Stop the remote microphone and wait for its pipe to close."""

        stream = self._mic_stream
        if stream is None or stream.source_id != node_id:
            raise HardwareNodeError("no microphone stream for node")
        reply = await self.send_command_and_wait_reply(
            node_id,
            {"cmd": "mic", "op": "stop"},
            timeout_ms=5000,
        )
        self._require_ok(reply, "microphone stop")
        try:
            await stream.wait_closed(MIC_CLOSE_TIMEOUT)
        finally:
            if self._mic_stream is stream and stream.is_closed:
                self._mic_stream = None

    async def set_pyro_enable(self, node_id, en):
        reply = await self.send_command_and_wait_reply(
            node_id,
            {"cmd": "pyro", "op": "enable", "enable": bool(en)},
            timeout_ms=3000,
        )
        self._require_ok(reply, "pyro enable")
        return bool(reply.get("enabled", en))

    async def get_pyro_state(self, node_id):
        """Return and remotely clear the pyro input's latched state."""

        reply = await self.send_command_and_wait_reply(
            node_id,
            {"cmd": "pyro", "op": "state"},
            timeout_ms=3000,
        )
        self._require_ok(reply, "pyro state")
        return bool(reply.get("triggered", False))

    async def on_command(self, src_id, command):
        if not isinstance(command, dict) or command.get("cmd") != "speaker" \
                or command.get("op") != "pull":
            return {"ok": False, "err": "unsupported"}

        transfer = self._speaker
        if transfer is None or src_id != transfer.node_id:
            return {"ok": False, "err": "no speaker transfer"}
        if command.get("pipe") != transfer.pipe_id:
            return {"ok": False, "err": "wrong pipe"}

        try:
            requested = int(command.get("bytes", 0))
        except (TypeError, ValueError):
            requested = 0
        remaining = len(transfer.data) - transfer.offset
        if requested <= 0 or requested > remaining:
            return {"ok": False, "err": "invalid size"}

        start = transfer.offset
        transfer.offset += requested
        final = transfer.offset == len(transfer.data)
        try:
            await self.send_pipe(
                transfer.pipe_id,
                transfer.data[start:transfer.offset],
                close=final,
            )
        except Exception as error:
            transfer.failure = HardwareNodeError(
                "speaker pipe send failed: {}".format(error)
            )
            transfer.all_sent.set()
            return {"ok": False, "err": "pipe send"}

        if final:
            transfer.all_sent.set()
        return {"ok": True, "bytes": requested}

    async def on_pipe_opened(self, pipe_id, src_id):
        stream = self._mic_stream
        if stream is not None and src_id == stream.source_id and \
                stream.pipe_id is None:
            stream._bind(pipe_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        stream = self._mic_stream
        if stream is not None and src_id == stream.source_id and \
                pipe_id == stream.pipe_id:
            stream._feed(data_chunk)

    async def on_pipe_closed(self, pipe_id, src_id):
        stream = self._mic_stream
        if stream is not None and src_id == stream.source_id and \
                pipe_id == stream.pipe_id:
            stream._finish()

    async def on_pipe_failed(self, pipe_id, src_id, reason,
                             transferred_bytes):
        stream = self._mic_stream
        if stream is not None and src_id == stream.source_id and \
                (stream.pipe_id is None or pipe_id == stream.pipe_id):
            stream._finish(MicStreamError(
                "microphone pipe failed: reason={}, bytes={}".format(
                    reason, transferred_bytes
                )
            ))

    async def close(self):
        stream = self._mic_stream
        if stream is not None:
            stream._finish(MicStreamError("PC transport closed"))
            self._mic_stream = None
        transfer = self._speaker
        if transfer is not None:
            transfer.failure = HardwareNodeError("PC transport closed")
            transfer.all_sent.set()
        await super().close()

    async def _close_speaker_pipe(self, transfer):
        if transfer.pipe_id is None or \
                transfer.pipe_id not in self._open_pipes:
            return
        try:
            await self.send_pipe(transfer.pipe_id, b"", close=True)
        except Exception:
            pass

    @staticmethod
    def _require_ok(reply, operation):
        if not isinstance(reply, dict):
            raise HardwareNodeError("{} returned an invalid reply".format(
                operation
            ))
        if not reply.get("ok"):
            reason = reply.get("err", "rejected")
            raise HardwareNodeError("{}: {}".format(operation, reason))


# A shorter alias for applications that already use "node" terminology.
HardwareNode = PCHardwareNode
