import asyncio
import json
import unittest

from usb_node_protocol import (
    FrameParser, encode_frame,
    GET_NODE_ID, GET_NODES_QTY, GET_NODE_INFO,
    SEND_COMMAND, SEND_COMMAND_WAIT, SEND_PIPE,
    RESULT, ON_COMMAND, ON_PIPE_DATA, ON_PIPE_CLOSED, ON_PIPE_FAILED,
    CALLBACK_RESULT,
)
from pc_transport_node import PCTransportNode
from pc_transport_node_async import AsyncPCTransportNode


class ScriptSerial:
    def __init__(self):
        self._parser = FrameParser()
        self._rx = bytearray()
        self.requests = []
        self.callback_results = []
        self.pipe_data = bytearray()
        self.closed = False

    @property
    def in_waiting(self):
        return len(self._rx)

    def read(self, count):
        data = bytes(self._rx[:count])
        del self._rx[:count]
        return data

    def write(self, data):
        data = bytes(data)
        for value in data:
            frame = self._parser.push(value)
            if frame is not None:
                self._handle(frame)
        return len(data)

    def close(self):
        self.closed = True

    def _queue(self, frame_type, request_id, payload=b""):
        self._rx.extend(encode_frame(frame_type, request_id, payload))

    def _handle(self, frame):
        frame_type, request_id, payload = frame
        self.requests.append(frame)

        if frame_type == CALLBACK_RESULT:
            self.callback_results.append((request_id, payload))
            return
        if frame_type == GET_NODE_ID:
            self._queue(RESULT, request_id, b"\x03")
        elif frame_type == GET_NODES_QTY:
            self._queue(RESULT, request_id, b"\x04")
        elif frame_type == GET_NODE_INFO:
            info = {"uuid": "0011223344556677", "id": payload[0]}
            self._queue(RESULT, request_id, json.dumps(info).encode())
        elif frame_type == SEND_COMMAND_WAIT:
            event = bytes((2,)) + json.dumps({"ask": 7}).encode()
            self._queue(ON_COMMAND, 91, event)
            self._queue(
                RESULT, request_id, json.dumps({"remote": True}).encode()
            )
        elif frame_type == SEND_COMMAND:
            self._queue(RESULT, request_id)
        elif frame_type == SEND_PIPE:
            self.pipe_data.extend(payload[2:])
            self._queue(RESULT, request_id)
        else:
            self._queue(RESULT, request_id)


class ProxyNode(PCTransportNode):
    def __init__(self, serial_port):
        super().__init__(serial_port=serial_port, timeout=0.2)
        self.commands = []
        self.deferred_done = []
        self.errors = []

    def on_command(self, src_id, command):
        self.commands.append((src_id, command))
        self.defer(
            self.send_command,
            4,
            {"deferred": True},
            on_result=self.deferred_done.append,
        )
        return {"answer": command["ask"] + 1}

    def on_callback_error(self, error):
        self.errors.append(error)


class AsyncScriptReader:
    def __init__(self, data):
        self.data = bytearray(data)

    async def read(self, count):
        if self.data:
            chunk = bytes(self.data[:count])
            del self.data[:count]
            return chunk
        await asyncio.Future()


class AsyncScriptWriter:
    def __init__(self):
        self.tx = bytearray()
        self.closed = False

    def write(self, data):
        self.tx.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class OrderedAsyncNode(AsyncPCTransportNode):
    def __init__(self, reader, writer):
        self.callbacks = []
        self.closed_callback = asyncio.Event()
        self.failed_callback = asyncio.Event()
        super().__init__(reader, writer)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        await asyncio.sleep(0.01)
        self.callbacks.append(("data", pipe_id, src_id, data_chunk))

    async def on_pipe_closed(self, pipe_id, src_id):
        self.callbacks.append(("closed", pipe_id, src_id))
        self.closed_callback.set()

    async def on_pipe_failed(self, pipe_id, src_id, reason,
                             transferred_bytes):
        self.callbacks.append((
            "failed", pipe_id, src_id, reason, transferred_bytes
        ))
        self.failed_callback.set()


class USBProtocolTests(unittest.TestCase):
    def test_binary_frame_fragmentation_and_resynchronization(self):
        parser = FrameParser()
        expected = b"\x00\x01\x02\x03\x80\xff"
        encoded = encode_frame(SEND_PIPE, 17, expected)

        result = None
        for value in encoded:
            result = parser.push(value) or result
        self.assertEqual(result, (SEND_PIPE, 17, expected))

        result = None
        for value in b"garbage" + encoded:
            result = parser.push(value) or result
        self.assertEqual(result, (SEND_PIPE, 17, expected))

    def test_proxy_api_callbacks_deferral_and_binary_pipe(self):
        serial_port = ScriptSerial()
        node = ProxyNode(serial_port)

        self.assertEqual(node.get_node_id(), 3)
        self.assertEqual(node.node_id, 3)
        self.assertEqual(node.get_nodes_qty(), 4)
        self.assertEqual(node.get_node_info(2)["id"], 2)

        reply = node.send_command_and_wait_reply(2, {"value": 1})
        self.assertEqual(reply, {"remote": True})
        self.assertEqual(node.commands, [(2, {"ask": 7})])
        self.assertEqual(node.errors, [])
        self.assertEqual(len(serial_port.callback_results), 1)
        callback_payload = serial_port.callback_results[0][1]
        self.assertEqual(callback_payload[0], 1)
        self.assertEqual(
            json.loads(callback_payload[1:]), {"answer": 8}
        )

        deferred = [
            request for request in serial_port.requests
            if request[0] == SEND_COMMAND
        ]
        self.assertEqual(len(deferred), 1)
        self.assertEqual(node.deferred_done, [True])

        pipe_bytes = bytes(range(256)) + b"\x00\x03\xff"
        node.send_pipe(9, pipe_bytes, close=True)
        self.assertEqual(bytes(serial_port.pipe_data), pipe_bytes)

        node.close()
        self.assertTrue(serial_port.closed)


class AsyncUSBProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipe_failure_callback_decodes_reason_and_byte_count(self):
        payload = bytes((7, 2)) + (6).to_bytes(4, "little") + \
            (12345).to_bytes(4, "little")
        writer = AsyncScriptWriter()
        node = OrderedAsyncNode(
            AsyncScriptReader(encode_frame(ON_PIPE_FAILED, 3, payload)),
            writer,
        )
        try:
            await asyncio.wait_for(node.failed_callback.wait(), timeout=1)
            self.assertEqual(node.callbacks, [
                ("failed", 7, 2, 6, 12345),
            ])
        finally:
            await node.close()

    async def test_pipe_callbacks_are_dispatched_in_wire_order(self):
        stream = encode_frame(ON_PIPE_DATA, 1, b"\x07\x02abc") + \
            encode_frame(ON_PIPE_CLOSED, 2, b"\x07\x02")
        writer = AsyncScriptWriter()
        node = OrderedAsyncNode(AsyncScriptReader(stream), writer)
        try:
            await asyncio.wait_for(node.closed_callback.wait(), timeout=1)
            self.assertEqual(node.callbacks, [
                ("data", 7, 2, b"abc"),
                ("closed", 7, 2),
            ])
        finally:
            await node.close()
        self.assertTrue(writer.closed)


if __name__ == "__main__":
    unittest.main()
