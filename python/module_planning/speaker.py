import time
import uasyncio as asyncio
from pyb import DAC, Pin

SAMPLE_RATE = 16000
BUFFER_SAMPLES = 2048
BUFFER_BYTES = BUFFER_SAMPLES * 2
OPEN_TIMEOUT_MS = 5000
CHUNK_TIMEOUT_MS = 5000


async def delay_until(deadline_us):
    while True:
        remaining = time.ticks_diff(deadline_us, time.ticks_us())
        if remaining <= 0:
            return
        await asyncio.sleep_ms(min(20, max(0, remaining // 1000)))


class Speaker:
    def __init__(self):
        self._power = Pin("C15", Pin.OUT)
        self._power.off()
        self._dac = DAC(Pin("A4"), bits=12, buffering=True)
        self._buffers = (bytearray(BUFFER_BYTES), bytearray(BUFFER_BYTES))
        self._state = "idle"
        self._owner = None
        self._pipe_id = None
        self._expected = self._received = 0
        self._pipe_open = asyncio.Event()
        self._data_ready = asyncio.Event()
        self._pipe_closed = False
        self._pipe_failure = None
        self._fill_buffer = None
        self._fill_length = self._fill_written = 0
        self._task = None
        self._abort = False
        self._last_error = None

    async def handle_command(self, node, src_id, operation, command):
        if operation == "play":
            total = command.get("bytes", 0)
            if not isinstance(total, int) or total <= 0 or total & 1:
                return {"err": "bytes"}
            if command.get("rate", SAMPLE_RATE) != SAMPLE_RATE:
                return {"err": "rate"}
            if self._state != "idle":
                return {"err": "busy"}
            self._owner = src_id
            self._expected = total
            self._received = 0
            self._pipe_id = None
            self._pipe_closed = False
            self._pipe_failure = self._last_error = None
            self._abort = False
            self._pipe_open.clear()
            self._data_ready.clear()
            self._state = "waiting"
            self._task = asyncio.create_task(self._run(node))
            return {"ok": True, "bytes": total, "rate": SAMPLE_RATE,
                    "chunk": BUFFER_BYTES}
        if operation == "state":
            return {"ok": True, "state": self._state,
                    "received": self._received, "error": self._last_error}
        if operation == "abort":
            if self._state != "idle" and src_id != self._owner:
                return {"err": "owner"}
            self._abort = True
            self._data_ready.set()
            self._pipe_open.set()
            return {"ok": True}
        return {"err": "op"}

    async def on_pipe_opened(self, pipe_id, src_id):
        if self._state == "waiting" and src_id == self._owner:
            self._pipe_id = pipe_id
            self._pipe_open.set()

    async def on_pipe_data(self, pipe_id, src_id, data):
        if pipe_id != self._pipe_id or src_id != self._owner or \
                self._fill_buffer is None:
            return
        remaining = self._fill_length - self._fill_written
        if len(data) > remaining or self._received + len(data) > self._expected:
            self._pipe_failure = "extra data"
            self._data_ready.set()
            return
        start = self._fill_written
        self._fill_buffer[start:start + len(data)] = data
        self._fill_written += len(data)
        self._received += len(data)
        if self._fill_written == self._fill_length:
            self._data_ready.set()

    async def on_pipe_closed(self, pipe_id, src_id):
        if pipe_id == self._pipe_id and src_id == self._owner:
            self._pipe_closed = True
            self._data_ready.set()

    async def on_pipe_failed(self, pipe_id, src_id, reason, transferred):
        if pipe_id == self._pipe_id and src_id == self._owner:
            self._pipe_failure = "pipe %d at %d" % (reason, transferred)
            self._data_ready.set()

    async def _wait_event(self, event, timeout_ms, label):
        started = time.ticks_ms()
        while not event.is_set():
            if self._abort:
                raise RuntimeError("aborted")
            if time.ticks_diff(time.ticks_ms(), started) >= timeout_ms:
                raise RuntimeError(label + " timeout")
            await asyncio.sleep_ms(1)

    async def _load(self, node, buffer):
        remaining = self._expected - self._received
        if remaining <= 0:
            return 0
        count = min(BUFFER_BYTES, remaining)
        self._fill_buffer = buffer
        self._fill_length = count
        self._fill_written = 0
        self._data_ready.clear()
        try:
            reply = await node.send_command_and_wait_reply(
                self._owner,
                {"cmd": "speaker", "op": "pull", "pipe": self._pipe_id,
                 "bytes": count}, timeout_ms=CHUNK_TIMEOUT_MS)
        except Exception:
            # The nested PC callback can finish the pipe transfer even if
            # its small command reply is subsequently lost.
            if self._fill_written != count:
                raise
            reply = {"ok": True}
        # Exact data can arrive before a delayed pull reply.
        if (not isinstance(reply, dict) or not reply.get("ok")) and \
                self._fill_written != count:
            raise RuntimeError("pull rejected")
        await self._wait_event(self._data_ready, CHUNK_TIMEOUT_MS, "data")
        self._fill_buffer = None
        if self._pipe_failure:
            raise RuntimeError(self._pipe_failure)
        if self._fill_written != count:
            raise RuntimeError("short data")
        return count

    async def _run(self, node):
        try:
            await self._wait_event(self._pipe_open, OPEN_TIMEOUT_MS, "pipe open")
            self._state = "preloading"
            sizes = [await self._load(node, self._buffers[0]),
                     await self._load(node, self._buffers[1])]
            self._power.on()
            self._state = "playing"
            current = 0
            refill = None
            while sizes[current]:
                size = sizes[current]
                started = time.ticks_us()
                self._dac.write_timed(memoryview(self._buffers[current])[:size],
                                      SAMPLE_RATE, mode=DAC.NORMAL)
                deadline = time.ticks_add(
                    started, (size // 2) * 1000000 // SAMPLE_RATE)
                if refill is not None:
                    sizes[refill] = await self._load(
                        node, self._buffers[refill])
                following = current ^ 1
                await delay_until(deadline)
                if not sizes[following]:
                    break
                refill = current
                current = following
            if self._received != self._expected:
                raise RuntimeError("short stream")
            if not self._pipe_closed:
                self._data_ready.clear()
                await self._wait_event(self._data_ready, CHUNK_TIMEOUT_MS,
                                       "pipe close")
            if self._pipe_failure:
                raise RuntimeError(self._pipe_failure)
            if not self._pipe_closed:
                raise RuntimeError("pipe not closed")
        except Exception as error:
            self._last_error = str(error)
            print("SPK!", error)
        finally:
            self._power.off()
            self._fill_buffer = None
            self._pipe_id = self._owner = None
            self._state = "idle"
            self._task = None

    def shutdown(self):
        self._abort = True
        self._data_ready.set()
        self._pipe_open.set()
        self._power.off()
