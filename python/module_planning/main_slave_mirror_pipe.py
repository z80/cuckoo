import asyncio
import ustruct
import utime

from transport_node import TransportNode


STREAM_COMMAND = "stream_test"
STREAM_CHUNK_SIZE = 512
STREAM_BYTE = 0xA5
STREAM_CHUNK = bytes((STREAM_BYTE,)) * STREAM_CHUNK_SIZE
STREAM_START_TIMEOUT_MS = 15000

# Diagnostic records are deliberately fixed-size and RAM-only.  They are read
# after a test, so collecting them does not add prints or RF traffic to the hot
# data path.
DIAG_RESET_COMMAND = "diag_reset"
DIAG_INFO_COMMAND = "diag_info"
DIAG_READ_COMMAND = "diag_read"
DIAG_CAPTURE_COMMAND = "diag_capture"
DIAG_STATS_COUNT = 23
DIAG_META_COUNT = 15
DIAG_RECORD_VALUES = DIAG_META_COUNT + DIAG_STATS_COUNT
DIAG_RECORD_SIZE = DIAG_RECORD_VALUES * 4
DIAG_RECORDS = 5
DIAG_PAGE_VALUES = 7

DIAG_BASELINE = 1
DIAG_PIPE_OPENED = 2
DIAG_DATA_SUBMITTED = 3
DIAG_PIPE_CLOSED = 4
DIAG_STREAM_FAILED = 5
DIAG_REMOTE_CAPTURE = 6

_UINT32 = "<I"
_NO_PIPE = 0xffffffff


class SlaveNode(TransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream_active = False
        self._diag_data = bytearray(DIAG_RECORD_SIZE * DIAG_RECORDS)
        self._diag_generation = 0
        self._diag_count = 0
        self._diag_overflow = False
        self._diag_error = ""
        self._diag_open_wait = 0
        self._diag_max_wait = 0
        self._diag_max_wait_at = 0
        self._diag_wait_20 = 0
        self._diag_wait_100 = 0
        self._diag_wait_500 = 0
        self._diag_wait_1000 = 0

    def _diag_reset(self):
        self._diag_generation = (self._diag_generation + 1) & 0xffffffff
        self._diag_count = 0
        self._diag_overflow = False
        self._diag_error = ""
        self._diag_open_wait = 0
        self._diag_max_wait = 0
        self._diag_max_wait_at = 0
        self._diag_wait_20 = 0
        self._diag_wait_100 = 0
        self._diag_wait_500 = 0
        self._diag_wait_1000 = 0
        return self._diag_generation

    def _diag_send_wait(self, elapsed, offset):
        if elapsed > self._diag_max_wait:
            self._diag_max_wait = elapsed
            self._diag_max_wait_at = offset
        if elapsed >= 20:
            self._diag_wait_20 += 1
        if elapsed >= 100:
            self._diag_wait_100 += 1
        if elapsed >= 500:
            self._diag_wait_500 += 1
        if elapsed >= 1000:
            self._diag_wait_1000 += 1

    def _diag_snapshot(self, tag, pipe_id, submitted):
        if self._diag_count >= DIAG_RECORDS:
            self._diag_overflow = True
            return False

        stats = self.core.stats()
        if len(stats) != DIAG_STATS_COUNT:
            self._diag_overflow = True
            return False
        schedule = self.core.get_radio_schedule()
        values = (
            self._diag_generation,
            tag,
            utime.ticks_ms(),
            _NO_PIPE if pipe_id is None else pipe_id,
            submitted,
            self.core.sticky_errors(),
            schedule[0],
            schedule[1],
            self._diag_open_wait,
            self._diag_max_wait,
            self._diag_max_wait_at,
            self._diag_wait_20,
            self._diag_wait_100,
            self._diag_wait_500,
            self._diag_wait_1000,
        )
        offset = self._diag_count * DIAG_RECORD_SIZE
        index = 0
        for value in values:
            ustruct.pack_into(_UINT32, self._diag_data,
                              offset + index * 4, value & 0xffffffff)
            index += 1
        for value in stats:
            ustruct.pack_into(_UINT32, self._diag_data,
                              offset + index * 4, value & 0xffffffff)
            index += 1
        self._diag_count += 1
        return True

    def _diag_record_page(self, index, page):
        if not isinstance(index, int) or index < 0 or \
                index >= self._diag_count or not isinstance(page, int) or \
                page < 0:
            return None
        first = page * DIAG_PAGE_VALUES
        if first >= DIAG_RECORD_VALUES:
            return None
        last = min(first + DIAG_PAGE_VALUES, DIAG_RECORD_VALUES)
        offset = index * DIAG_RECORD_SIZE
        result = []
        for value_index in range(first, last):
            result.append(ustruct.unpack_from(
                _UINT32, self._diag_data, offset + value_index * 4
            )[0])
        return result

    async def on_command(self, src_id, command):
        if not isinstance(command, dict):
            return {"err": "command"}

        command_name = command.get("cmd")
        if command_name == DIAG_RESET_COMMAND:
            if self._stream_active:
                return {"err": "busy"}
            return {"ok": True, "g": self._diag_reset()}
        if command_name == DIAG_INFO_COMMAND:
            return {
                "g": self._diag_generation,
                "n": self._diag_count,
                "o": self._diag_overflow,
                "active": self._stream_active,
                "err": self._diag_error,
            }
        if command_name == DIAG_READ_COMMAND:
            record = self._diag_record_page(
                command.get("i"), command.get("p", 0)
            )
            if record is None:
                return {"err": "index"}
            return {
                "g": self._diag_generation,
                "i": command.get("i"),
                "p": command.get("p", 0),
                "r": record,
            }
        if command_name == DIAG_CAPTURE_COMMAND:
            saved = self._diag_snapshot(
                DIAG_REMOTE_CAPTURE, None, command.get("bytes", 0)
            )
            return {"ok": saved, "g": self._diag_generation,
                    "n": self._diag_count}
        if command_name != STREAM_COMMAND:
            return {"err": "command"}

        total = command.get("bytes")
        if not isinstance(total, int) or total <= 0:
            return {"err": "bytes"}
        if self._stream_active:
            return {"err": "busy"}

        self._stream_active = True
        asyncio.create_task(self._send_test_stream(src_id, total))
        return {
            "ok": True,
            "bytes": total,
            "chunk": STREAM_CHUNK_SIZE,
            "value": STREAM_BYTE,
        }

    async def _send_test_stream(self, destination, total):
        pipe_id = None
        sent = 0
        try:
            # Let the command reply finish before starting another outbound
            # operation. Command latency is not part of the measurement.
            await asyncio.sleep(0.05)
            waiting_started = utime.ticks_ms()
            while self._transport_busy():
                if utime.ticks_diff(utime.ticks_ms(), waiting_started) >= \
                        STREAM_START_TIMEOUT_MS:
                    raise RuntimeError("transport remained busy")
                await asyncio.sleep(0.001)

            self._diag_snapshot(DIAG_BASELINE, None, sent)
            started = utime.ticks_ms()
            pipe_id = await self.open_pipe(destination)
            self._diag_open_wait = utime.ticks_diff(
                utime.ticks_ms(), started
            )
            self._diag_snapshot(DIAG_PIPE_OPENED, pipe_id, sent)
            print("STREAM", pipe_id, "to", destination, "bytes", total)

            while sent < total:
                count = min(STREAM_CHUNK_SIZE, total - sent)
                started = utime.ticks_ms()
                await self.send_pipe(pipe_id, STREAM_CHUNK[:count])
                elapsed = utime.ticks_diff(utime.ticks_ms(), started)
                self._diag_send_wait(elapsed, sent)
                sent += count

            self._diag_snapshot(DIAG_DATA_SUBMITTED, pipe_id, sent)
            await self.send_pipe(pipe_id, b"", close=True)
            self._diag_snapshot(DIAG_PIPE_CLOSED, pipe_id, sent)
            print("STREAM done", sent)
        except Exception as error:
            # Keep diag_info comfortably below the 128-byte command-reply
            # limit even if the exception contains escapable characters.
            self._diag_error = str(error)[:32]
            self._diag_snapshot(DIAG_STREAM_FAILED, pipe_id, sent)
            print("STREAM!", sent, error)
        finally:
            self._stream_active = False


async def async_main():
    node = SlaveNode()
    await node.process()


def main():
    asyncio.run(async_main())


main()
