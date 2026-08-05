import asyncio
import importlib
import json
import sys
import types
import unittest

from usb_node_protocol import (
    FrameParser, encode_frame,
    SEND_COMMAND_WAIT, RESULT, ON_COMMAND, ON_PIPE_CLOSED, CALLBACK_RESULT,
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


def import_bridge():
    uasyncio = types.ModuleType("uasyncio")
    uasyncio.Lock = asyncio.Lock
    uasyncio.create_task = asyncio.create_task

    async def sleep_ms(milliseconds):
        await asyncio.sleep(0)

    uasyncio.sleep_ms = sleep_ms
    sys.modules["uasyncio"] = uasyncio
    sys.modules["ujson"] = json
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

    async def send_command_and_wait_reply(self, node_id, command,
                                          timeout_ms=2000):
        return await self.on_command(node_id, command)


class MCUBridgeTests(unittest.IsolatedAsyncioTestCase):
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
