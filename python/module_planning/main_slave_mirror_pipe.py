import asyncio
import utime

from transport_node import TransportNode


STREAM_COMMAND = "stream_test"
STREAM_CHUNK_SIZE = 512
STREAM_BYTE = 0xA5
STREAM_CHUNK = bytes((STREAM_BYTE,)) * STREAM_CHUNK_SIZE
STREAM_START_TIMEOUT_MS = 15000


class SlaveNode(TransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream_active = False

    async def on_command(self, src_id, command):
        if not isinstance(command, dict) or \
                command.get("cmd") != STREAM_COMMAND:
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

            pipe_id = await self.open_pipe(destination)
            print("STREAM", pipe_id, "to", destination, "bytes", total)

            while sent < total:
                count = min(STREAM_CHUNK_SIZE, total - sent)
                await self.send_pipe(pipe_id, STREAM_CHUNK[:count])
                sent += count

            await self.send_pipe(pipe_id, b"", close=True)
            print("STREAM done", sent)
        except Exception as error:
            print("STREAM!", sent, error)
        finally:
            self._stream_active = False


async def async_main():
    node = SlaveNode()
    await node.process()


def main():
    asyncio.run(async_main())


main()
