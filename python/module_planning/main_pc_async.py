import asyncio
import os
import sys
import time

if os.name == "nt":
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)

from pc_transport_node_async import AsyncPCTransportNode


PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"
TEST_BYTES = int(sys.argv[2]) if len(sys.argv) > 2 else 256 * 1024
STREAM_COMMAND = "stream_test"
STREAM_BYTE = 0xA5


class StreamFailure(RuntimeError):
    pass


class AsyncPCNode(AsyncPCTransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_source = None
        self.expected_bytes = 0
        self.received_bytes = 0
        self.invalid_bytes = 0
        self.stream_started_at = None
        self.stream_finished_at = None
        self.active_pipe_id = None
        self.failure = None
        self.stream_opened = asyncio.Event()
        self.stream_finished = asyncio.Event()
        self.stream_closed = asyncio.Event()
        self.stream_failed = asyncio.Event()

    async def on_command(self, src_id, command):
        return {"ok": True}

    async def on_pipe_opened(self, pipe_id, src_id):
        if src_id != self.test_source or self.active_pipe_id is not None:
            return
        self.active_pipe_id = pipe_id
        self.received_bytes = 0
        self.invalid_bytes = 0
        self.stream_started_at = time.perf_counter()
        self.stream_opened.set()
        print("pipe", pipe_id, "opened from", src_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        if src_id != self.test_source or pipe_id != self.active_pipe_id or \
                self.stream_started_at is None:
            return
        remaining = self.expected_bytes - self.received_bytes
        if remaining <= 0:
            return
        data = data_chunk[:remaining]
        self.received_bytes += len(data)
        self.invalid_bytes += len(data) - data.count(STREAM_BYTE)
        if self.received_bytes >= self.expected_bytes and \
                self.stream_finished_at is None:
            self.stream_finished_at = time.perf_counter()
            print("data complete after {:.3} s".format(
                self.stream_finished_at - self.stream_started_at
            ))
            self.stream_finished.set()

    async def on_pipe_closed(self, pipe_id, src_id):
        if src_id == self.test_source and pipe_id == self.active_pipe_id:
            now = time.perf_counter()
            print("pipe", pipe_id, "closed from", src_id)
            if self.stream_finished_at is not None:
                print("close delay: {:.3f} s".format(
                    now - self.stream_finished_at
                ))
            else:
                self.failure = (
                    "pipe closed before all data", self.received_bytes
                )
                self.stream_failed.set()
            self.stream_closed.set()

    async def on_pipe_failed(self, pipe_id, src_id, reason,
                             transferred_bytes):
        if src_id != self.test_source:
            return
        if self.active_pipe_id is not None and \
                pipe_id != self.active_pipe_id:
            return
        self.failure = ("pipe failed", reason, transferred_bytes)
        print("pipe", pipe_id, "failed from", src_id, "reason", reason,
              "bytes", transferred_bytes)
        self.stream_failed.set()

    def on_callback_error(self, error):
        print("callback error:", error)


async def find_target(node):
    quantity = await node.get_nodes_qty()
    print("online nodes:", quantity)
    for index in range(quantity):
        info = await node.get_node_info(index)
        print("node", index, info)
        node_id = info.get("id") if info else None
        if node_id is not None and node_id != node.node_id:
            return node_id
    return None


async def wait_for_stream(node, event, timeout):
    event_task = asyncio.create_task(event.wait())
    failure_task = asyncio.create_task(node.stream_failed.wait())
    try:
        done, unused_pending = await asyncio.wait(
            (event_task, failure_task), timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise asyncio.TimeoutError
        if node.failure is not None:
            raise StreamFailure(str(node.failure))
    finally:
        for task in (event_task, failure_task):
            if not task.done():
                task.cancel()


async def main():
    node = await AsyncPCNode.create(port=PORT)
    phase = "setup"
    try:
        while await node.get_node_id() is None:
            print("waiting for NRF registration")
            await asyncio.sleep(1)
        print("PC node ID:", node.node_id)

        target = await find_target(node)
        if target is None:
            print("No remote node available")
            return

        node.test_source = target
        node.expected_bytes = TEST_BYTES
        print("requesting", TEST_BYTES, "bytes from node", target)
        reply = await node.send_command_and_wait_reply(
            target,
            {"cmd": STREAM_COMMAND, "bytes": TEST_BYTES},
            timeout_ms=5000,
        )
        print("command reply:", reply)
        if not reply.get("ok"):
            return

        phase = "pipe open"
        await wait_for_stream(node, node.stream_opened, 5)
        timeout = max(15.0, TEST_BYTES / 4000.0)
        phase = "pipe data"
        await wait_for_stream(node, node.stream_finished, timeout)
        phase = "pipe close"
        await wait_for_stream(node, node.stream_closed, 15)

        elapsed = node.stream_finished_at - node.stream_started_at
        byte_rate = node.received_bytes / elapsed
        bit_rate = byte_rate * 8
        print("received:", node.received_bytes, "bytes")
        print("invalid:", node.invalid_bytes, "bytes")
        print("elapsed: {:.3f} s".format(elapsed))
        print("throughput: {:.1f} B/s ({:.1f} kbit/s)".format(
            byte_rate, bit_rate / 1000.0
        ))
    except asyncio.TimeoutError:
        print("TEST! timeout waiting for", phase, "after",
              node.received_bytes, "bytes")
    except StreamFailure as error:
        print("TEST!", error, "after", node.received_bytes, "bytes")
    except asyncio.CancelledError:
        pass
    finally:
        await node.close()


if __name__ == "__main__":
    try:
        while True:
            asyncio.run(main())
    except KeyboardInterrupt:
        pass
