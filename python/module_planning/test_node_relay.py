import asyncio
import unittest
import uuid

import zmq.asyncio

from node_relay import create_remote_node
from node_relay.connection import (
    RelayBusyError,
    RelayServerEndpoint,
    RelayTimeoutError,
    RemoteRelayError,
)
from pc_hardware_node import MicStreamError
from node_relay.protocol import (
    HARDWARE,
    TRANSPORT,
    decode_message,
    encode_message,
)
from node_relay.remote_hardware import MAX_PLAY_BYTES, RemoteHardwareNode
from node_relay.remote_transport import RemoteTransportNode
from node_relay.server import HardwareNodeRelayServer, TransportNodeRelayServer


class FakeTransport:
    def __init__(self):
        self.node_id = 7
        self._open_pipes = set()
        self.pipe_data = bytearray()
        self.closed = False

    async def get_node_id(self):
        return self.node_id

    async def get_nodes_qty(self):
        await asyncio.sleep(0)
        return 2

    async def get_node_info(self, index):
        return {"uuid": "test", "id": index}

    async def get_core_diagnostics(self):
        return {"stats": (1, 2), "sticky_errors": 0,
                "radio_schedule": (20, 3)}

    async def send_command(self, node_id, command):
        return True

    async def send_command_and_wait_reply(
        self, node_id, command, timeout_ms=2000
    ):
        return {"node": node_id, "command": command}

    async def open_pipe(self, node_id):
        self._open_pipes.add(4)
        return 4

    async def send_pipe(self, pipe_id, data, close=False):
        self.pipe_data.extend(data)
        if close:
            self._open_pipes.discard(pipe_id)

    async def send_pipe_streamed(self, pipe_id, data, close=False):
        await self.send_pipe(pipe_id, data, close=close)

    async def close(self):
        self.closed = True


class FakeMicStream:
    def __init__(self, source_id):
        self.source_id = source_id
        self.pipe_id = 9
        self.overrun_count = 0
        self.dropped_bytes = 0
        self._queue = asyncio.Queue()

    async def read(self):
        value = await self._queue.get()
        if isinstance(value, Exception):
            raise value
        return value

    def feed(self, data):
        self._queue.put_nowait(bytes(data))

    def finish(self):
        self._queue.put_nowait(b"")


class FakeHardware(FakeTransport):
    def __init__(self):
        super().__init__()
        self.played = None
        self.mic = None
        self.pyro_enabled = False
        self.pyro_triggered = True

    async def play_buffer(self, node_id, data):
        self.played = (node_id, bytes(data))

    async def start_mic_stream(self, node_id):
        self.mic = FakeMicStream(node_id)
        return self.mic

    async def stop_mic_stream(self, node_id):
        if self.mic is not None:
            self.mic.finish()

    async def set_pyro_enable(self, node_id, enabled):
        self.pyro_enabled = bool(enabled)
        return self.pyro_enabled

    async def get_pyro_state(self, node_id):
        value = self.pyro_triggered
        self.pyro_triggered = False
        return value


class CallbackRemote(RemoteTransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = []

    async def on_command(self, src_id, command):
        quantity = await self.get_nodes_qty()
        self.events.append(("command", src_id, command))
        return {"quantity": quantity}

    async def on_command_completed(self, src_id, command, result):
        self.events.append(("completed", src_id, command, result))

    async def on_pipe_data(self, pipe_id, src_id, data):
        self.events.append(("data", pipe_id, src_id, bytes(data)))


class RelayTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.context = zmq.asyncio.Context()
        self.endpoint_name = "inproc://relay-{}".format(uuid.uuid4().hex)
        self.server = None
        self.node = None

    async def asyncTearDown(self):
        if self.node is not None:
            await self.node.close()
        if self.server is not None:
            await self.server.close()
        self.context.term()

    async def start_transport(self, node_type=RemoteTransportNode):
        backend = FakeTransport()
        endpoint = await RelayServerEndpoint.bind(
            self.endpoint_name, TRANSPORT, context=self.context
        )
        self.server = TransportNodeRelayServer(endpoint, backend)
        self.node = await node_type.connect(
            self.endpoint_name, context=self.context
        )
        return backend

    async def start_hardware(self):
        backend = FakeHardware()
        endpoint = await RelayServerEndpoint.bind(
            self.endpoint_name, HARDWARE, context=self.context
        )
        self.server = HardwareNodeRelayServer(endpoint, backend)
        self.node = await RemoteHardwareNode.connect(
            self.endpoint_name, context=self.context
        )
        return backend

    async def test_protocol_preserves_binary_frame(self):
        data = bytes(range(256))
        message = decode_message(encode_message(
            5, 0x12345678, {"operation": "binary"}, data
        ))
        self.assertEqual(message.id, 0x12345678)
        self.assertEqual(message.data, data)

    async def test_factory_rejects_unknown_service(self):
        with self.assertRaises(ValueError):
            await create_remote_node("invalid", self.endpoint_name)

    async def test_service_mismatch_is_rejected(self):
        endpoint = await RelayServerEndpoint.bind(
            self.endpoint_name, TRANSPORT, context=self.context
        )
        self.server = TransportNodeRelayServer(endpoint, FakeTransport())
        with self.assertRaises(RemoteRelayError):
            await RemoteHardwareNode.connect(
                self.endpoint_name, context=self.context
            )

    async def test_transport_api_and_streamed_binary(self):
        backend = await self.start_transport()
        self.assertEqual(await self.node.get_node_id(), 7)
        self.assertEqual(self.node.node_id, 7)
        self.assertEqual(await self.node.get_nodes_qty(), 2)
        self.assertEqual((await self.node.get_node_info(1))["id"], 1)
        diagnostics = await self.node.get_core_diagnostics()
        self.assertEqual(diagnostics["stats"], (1, 2))
        reply = await self.node.send_command_and_wait_reply(
            3, {"test": True}
        )
        self.assertEqual(reply["node"], 3)
        pipe_id = await self.node.open_pipe(3)
        data = bytes(range(256)) * 4
        await self.node.send_pipe_streamed(pipe_id, data, close=True)
        self.assertEqual(bytes(backend.pipe_data), data)

    async def test_callback_can_make_nested_call_and_keeps_order(self):
        await self.start_transport(CallbackRemote)
        reply = await self.server.endpoint.callback(
            "on_command", {"src_id": 3, "command": {"ask": 1}}
        )
        self.assertEqual(reply.metadata["result"], {"quantity": 2})
        await self.server.endpoint.event("on_command_completed", {
            "src_id": 3,
            "command": {"ask": 1},
            "result": {"quantity": 2},
        })
        await self.server.endpoint.event(
            "on_pipe_data", {"pipe_id": 4, "src_id": 3}, b"abc"
        )
        async def wait_for_events():
            while len(self.node.events) < 3:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_events(), 1)
        self.assertEqual([event[0] for event in self.node.events], [
            "command", "completed", "data"
        ])

    async def test_callback_timeout_does_not_wedge_connection(self):
        class SlowRemote(CallbackRemote):
            async def on_command(self, src_id, command):
                await asyncio.sleep(1)

        backend = FakeTransport()
        endpoint = await RelayServerEndpoint.bind(
            self.endpoint_name,
            TRANSPORT,
            callback_timeout=0.05,
            context=self.context,
        )
        self.server = TransportNodeRelayServer(endpoint, backend)
        self.node = await SlowRemote.connect(
            self.endpoint_name, context=self.context
        )
        with self.assertRaises(RelayTimeoutError):
            await endpoint.callback(
                "on_command", {"src_id": 3, "command": {}}
            )
        self.assertEqual(await self.node.get_nodes_qty(), 2)

    async def test_second_client_is_rejected(self):
        await self.start_transport()
        with self.assertRaises(RelayBusyError):
            await RemoteTransportNode.connect(
                self.endpoint_name, context=self.context
            )

    async def test_client_can_reconnect_after_heartbeat_timeout(self):
        backend = FakeTransport()
        endpoint = await RelayServerEndpoint.bind(
            self.endpoint_name,
            TRANSPORT,
            heartbeat_interval=0.02,
            connection_timeout=0.1,
            context=self.context,
        )
        self.server = TransportNodeRelayServer(endpoint, backend)
        first = await RemoteTransportNode.connect(
            self.endpoint_name,
            heartbeat_interval=0.02,
            connection_timeout=0.1,
            context=self.context,
        )
        await first.close()
        await asyncio.sleep(0.15)
        self.node = await RemoteTransportNode.connect(
            self.endpoint_name,
            heartbeat_interval=0.02,
            connection_timeout=0.1,
            context=self.context,
        )
        self.assertEqual(await self.node.get_node_id(), 7)

    async def test_hardware_play_limit_and_pyro(self):
        backend = await self.start_hardware()
        samples = b"\x01\x00" * 32
        await self.node.play_buffer(3, samples)
        self.assertEqual(backend.played, (3, samples))
        self.assertTrue(await self.node.set_pyro_enable(3, True))
        self.assertTrue(await self.node.get_pyro_state(3))
        self.assertFalse(await self.node.get_pyro_state(3))
        with self.assertRaises(ValueError):
            await self.node.play_buffer(3, b"\x00" * (MAX_PLAY_BYTES + 2))

    async def test_hardware_accepts_exact_play_limit(self):
        backend = await self.start_hardware()
        data = b"\x00" * MAX_PLAY_BYTES
        await self.node.play_buffer(3, data)
        self.assertEqual(len(backend.played[1]), MAX_PLAY_BYTES)

    async def test_cancelled_playback_releases_server_call(self):
        class SlowHardware(FakeHardware):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.cancelled = asyncio.Event()

            async def play_buffer(self, node_id, data):
                self.started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise

        backend = SlowHardware()
        endpoint = await RelayServerEndpoint.bind(
            self.endpoint_name, HARDWARE, context=self.context
        )
        self.server = HardwareNodeRelayServer(endpoint, backend)
        self.node = await RemoteHardwareNode.connect(
            self.endpoint_name, context=self.context
        )
        playback = asyncio.create_task(
            self.node.play_buffer(3, b"\x00\x00")
        )
        await asyncio.wait_for(backend.started.wait(), 1)
        playback.cancel()
        await asyncio.gather(playback, return_exceptions=True)
        await asyncio.wait_for(backend.cancelled.wait(), 1)
        self.assertEqual(await self.node.get_nodes_qty(), 2)

    async def test_microphone_data_is_ordered_and_not_dropped(self):
        backend = await self.start_hardware()
        stream = await self.node.start_mic_stream(3)
        backend.mic.feed(b"abc")
        backend.mic.feed(b"def")
        self.assertEqual(await asyncio.wait_for(stream.read(), 1), b"abc")
        self.assertEqual(await asyncio.wait_for(stream.read(), 1), b"def")
        stop = asyncio.create_task(self.node.stop_mic_stream(3))
        await asyncio.wait_for(stop, 1)
        self.assertTrue(stream.is_closed)
        self.assertEqual(stream.overrun_count, 0)
        self.assertEqual(stream.dropped_bytes, 0)

    async def test_microphone_source_overrun_fails_explicitly(self):
        backend = await self.start_hardware()
        stream = await self.node.start_mic_stream(3)
        backend.mic.overrun_count = 1
        backend.mic.dropped_bytes = 32
        backend.mic.feed(b"lost")
        with self.assertRaises(MicStreamError):
            await asyncio.wait_for(stream.read(), 1)


if __name__ == "__main__":
    unittest.main()
