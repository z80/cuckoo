import asyncio
import importlib
import json
import struct
import sys
import types
import unittest

from usb_node_protocol import (
    FrameParser, encode_frame,
    GET_NODE_ID, SEND_COMMAND_WAIT, SEND_PIPE_STREAM,
    PIPE_STREAM_END, PIPE_STREAM_CLOSE,
    RESULT, ERROR, BUSY, ON_COMMAND, ON_PIPE_CLOSED, ON_PIPE_FAILED,
    CALLBACK_RESULT,
)


class FakeTicks(types.ModuleType):
    def __init__(self):
        super().__init__("utime")
        self.now = 0

    def ticks_ms(self):
        self.now += 1
        return self.now

    @staticmethod
    def ticks_diff(new, old):
        return new - old

    @staticmethod
    def ticks_add(value, delta):
        return value + delta


def import_bridge():
    uasyncio = types.ModuleType("uasyncio")
    uasyncio.Lock = asyncio.Lock
    uasyncio.create_task = asyncio.create_task

    async def sleep_ms(milliseconds):
        await asyncio.sleep(0)

    uasyncio.sleep_ms = sleep_ms
    sys.modules["uasyncio"] = uasyncio
    sys.modules["ujson"] = json
    sys.modules["ustruct"] = struct
    sys.modules["utime"] = FakeTicks()
    sys.modules.pop("usb_node_bridge", None)
    return importlib.import_module("usb_node_bridge")


class FakeUSB:
    def __init__(self):
        self.rx = bytearray()
        self.tx = bytearray()

    def readinto(self, buffer):
        if not self.rx:
            return None
        count = min(len(buffer), len(self.rx))
        buffer[:count] = self.rx[:count]
        del self.rx[:count]
        return count

    def write(self, data):
        data = bytes(data)
        self.tx.extend(data)
        return len(data)


class StallingUSB(FakeUSB):
    def __init__(self, stalled_writes):
        super().__init__()
        self.stalled_writes = stalled_writes

    def write(self, data):
        if self.stalled_writes:
            self.stalled_writes -= 1
            return None
        return super().write(data)

    @staticmethod
    def isconnected():
        return True


class FakeNode:
    def __init__(self):
        self.node_id = 3
        self.pipe_writes = []
        self.pipe_error_at = None

    async def send_command_and_wait_reply(self, node_id, command,
                                          timeout_ms=2000):
        return await self.on_command(node_id, command)

    async def send_pipe(self, pipe_id, data, close=False):
        if self.pipe_error_at == len(self.pipe_writes):
            raise RuntimeError("radio failed")
        self.pipe_writes.append((pipe_id, bytes(data), close))


def decode_frames(data):
    parser = FrameParser()
    frames = []
    for value in data:
        frame = parser.push(value)
        if frame is not None:
            frames.append(frame)
    return frames


class MCUBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_pipe_write_is_incremental_and_replies_at_end(self):
        bridge_module = import_bridge()
        usb = FakeUSB()
        node = FakeNode()
        bridge = bridge_module.USBNodeBridge(node, usb)

        await bridge._handle_frame(
            SEND_PIPE_STREAM, 9, b"\x07\x00first"
        )
        self.assertEqual(node.pipe_writes, [(7, b"first", False)])
        self.assertEqual(usb.tx, b"")
        self.assertTrue(bridge._request_active)

        await bridge._handle_frame(
            SEND_PIPE_STREAM, 9,
            bytes((7, PIPE_STREAM_END | PIPE_STREAM_CLOSE)) + b"last",
        )
        self.assertEqual(
            node.pipe_writes,
            [(7, b"first", False), (7, b"last", True)],
        )
        self.assertEqual(decode_frames(usb.tx), [(RESULT, 9, b"")])
        self.assertFalse(bridge._request_active)

    async def test_unrelated_request_is_busy_during_pipe_stream(self):
        bridge_module = import_bridge()
        usb = FakeUSB()
        bridge = bridge_module.USBNodeBridge(FakeNode(), usb)

        await bridge._handle_frame(SEND_PIPE_STREAM, 4, b"\x02\x00a")
        await bridge._handle_frame(GET_NODE_ID, 5, b"")

        self.assertEqual(decode_frames(usb.tx), [(BUSY, 5, b"")])

    async def test_stream_error_is_reported_once_then_discarded_through_end(self):
        bridge_module = import_bridge()
        usb = FakeUSB()
        node = FakeNode()
        node.pipe_error_at = 1
        bridge = bridge_module.USBNodeBridge(node, usb)

        await bridge._handle_frame(SEND_PIPE_STREAM, 6, b"\x03\x00ok")
        await bridge._handle_frame(SEND_PIPE_STREAM, 6, b"\x03\x00bad")
        await bridge._handle_frame(SEND_PIPE_STREAM, 6, b"\x03\x00drop")
        await bridge._handle_frame(
            SEND_PIPE_STREAM, 6,
            bytes((3, PIPE_STREAM_END | PIPE_STREAM_CLOSE)) + b"drop",
        )

        self.assertEqual(node.pipe_writes, [(3, b"ok", False)])
        frames = decode_frames(usb.tx)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0][0:2], (ERROR, 6))
        self.assertIn(b"radio failed", frames[0][2])
        self.assertFalse(bridge._request_active)

    async def test_stream_end_without_close_does_not_close_radio_pipe(self):
        bridge_module = import_bridge()
        usb = FakeUSB()
        node = FakeNode()
        bridge = bridge_module.USBNodeBridge(node, usb)

        await bridge._handle_frame(
            SEND_PIPE_STREAM, 7,
            bytes((8, PIPE_STREAM_END)) + b"data",
        )

        self.assertEqual(node.pipe_writes, [(8, b"data", False)])
        self.assertEqual(decode_frames(usb.tx), [(RESULT, 7, b"")])

    async def test_pipe_failure_callback_includes_reason_and_byte_count(self):
        bridge_module = import_bridge()
        usb = FakeUSB()
        bridge = bridge_module.USBNodeBridge(FakeNode(), usb)
        parser = FrameParser()

        await bridge.on_pipe_failed(7, 2, 6, 12345)

        frame = None
        for value in usb.tx:
            frame = parser.push(value) or frame
        self.assertEqual(frame[0:2], (ON_PIPE_FAILED, 1))
        self.assertEqual(struct.unpack("<BBII", frame[2]),
                         (7, 2, 6, 12345))

    async def test_pipe_close_callback_retries_after_usb_write_timeout(self):
        bridge_module = import_bridge()
        usb = StallingUSB(501)
        bridge = bridge_module.USBNodeBridge(FakeNode(), usb)
        parser = FrameParser()

        await bridge.on_pipe_closed(7, 2)

        frame = None
        for value in usb.tx:
            frame = parser.push(value) or frame
        self.assertEqual(frame, (ON_PIPE_CLOSED, 1, b"\x07\x02"))

    async def test_api_worker_accepts_callback_result_while_busy(self):
        bridge_module = import_bridge()
        usb = FakeUSB()
        node = FakeNode()
        bridge = bridge_module.USBNodeBridge(node, usb)
        parser = FrameParser()

        command = json.dumps({"self": True}).encode()
        payload = bytes((3, 0xD0, 0x07)) + command
        usb.rx.extend(encode_frame(SEND_COMMAND_WAIT, 12, payload))

        process_task = asyncio.create_task(bridge.process())
        try:
            event = None
            for unused in range(100):
                await asyncio.sleep(0)
                while usb.tx:
                    frame = parser.push(usb.tx.pop(0))
                    if frame is not None:
                        event = frame
                if event is not None:
                    break

            self.assertIsNotNone(event)
            self.assertEqual(event[0], ON_COMMAND)
            self.assertEqual(event[2][0], 3)
            self.assertEqual(json.loads(event[2][1:]), {"self": True})

            callback = b"\x01" + json.dumps({"ok": 8}).encode()
            usb.rx.extend(
                encode_frame(CALLBACK_RESULT, event[1], callback)
            )

            result = None
            for unused in range(100):
                await asyncio.sleep(0)
                while usb.tx:
                    frame = parser.push(usb.tx.pop(0))
                    if frame is not None:
                        result = frame
                if result is not None:
                    break

            self.assertIsNotNone(result)
            self.assertEqual(result[0], RESULT)
            self.assertEqual(result[1], 12)
            self.assertEqual(json.loads(result[2]), {"ok": 8})
        finally:
            process_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await process_task


if __name__ == "__main__":
    unittest.main()
