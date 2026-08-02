import asyncio
import os
import sys
import time

if os.name == "nt":
    import ctypes
    ctypes.windll.winmm.timeBeginPeriod(1)

from pc_transport_node_async import AsyncPCTransportNode


PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
TEST_BYTES = int(sys.argv[2]) if len(sys.argv) > 2 else 256 * 1024
STREAM_COMMAND = "stream_test"
STREAM_BYTE = 0xA5


class AsyncPCNode(AsyncPCTransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_source = None
        self.expected_bytes = 0
        self.received_bytes = 0
        self.invalid_bytes = 0
        self.stream_started_at = None
        self.stream_finished_at = None
        self.stream_opened = asyncio.Event()
        self.stream_finished = asyncio.Event()
        self.stream_closed = asyncio.Event()

    async def on_command(self, src_id, command):
        return {"ok": True}

    async def on_pipe_opened(self, pipe_id, src_id):
        if src_id != self.test_source or self.stream_started_at is not None:
            return
        self.received_bytes = 0
        self.invalid_bytes = 0
        self.stream_started_at = time.perf_counter()
        self.stream_opened.set()
        print("pipe", pipe_id, "opened from", src_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        if src_id != self.test_source or self.stream_started_at is None:
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
            self.stream_finished.set()

    async def on_pipe_closed(self, pipe_id, src_id):
        if src_id == self.test_source:
            print("pipe", pipe_id, "closed from", src_id)
            self.stream_closed.set()

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


async def main():
    node = await AsyncPCNode.create(port=PORT)
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

        await asyncio.wait_for(node.stream_opened.wait(), timeout=5)
        timeout = max(15.0, TEST_BYTES / 4000.0)
        await asyncio.wait_for(node.stream_finished.wait(), timeout=timeout)
        await asyncio.wait_for(node.stream_closed.wait(), timeout=5)

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
        print("TEST! timeout after", node.received_bytes, "bytes")
    except asyncio.CancelledError:
        pass
    finally:
        await node.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
