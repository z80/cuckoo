import asyncio
import json

from hardware_node.pc_hardware_node import PCHardwareNode
from pc_transport_node_async import AsyncPCTransportNode
from usb_node_protocol import CALLBACK_RESULT, ON_COMMAND


class FakeHardwareNode(PCHardwareNode):
    def __init__(self):
        self._speaker = None
        self._mic_stream = None
        self._open_pipes = set()
        self.sent = bytearray()
        self.pyro_triggered = True

    async def send_command_and_wait_reply(self, node_id, command,
                                          timeout_ms=2000):
        device = command["cmd"]
        operation = command["op"]
        if device == "speaker" and operation == "play":
            return {"ok": True}
        if device == "speaker" and operation == "state":
            return {"ok": True, "state": "idle", "error": None}
        if device == "mic" and operation == "start":
            asyncio.create_task(self.on_pipe_opened(9, node_id))
            return {"ok": True}
        if device == "mic" and operation == "stop":
            asyncio.create_task(self.on_pipe_closed(9, node_id))
            return {"ok": True}
        if device == "pyro" and operation == "enable":
            return {"ok": True, "enabled": command["enable"]}
        if device == "pyro" and operation == "state":
            value = self.pyro_triggered
            self.pyro_triggered = False
            return {"ok": True, "triggered": value}
        raise AssertionError(command)

    async def open_pipe(self, node_id):
        pipe_id = 7
        self._open_pipes.add(pipe_id)

        async def dispatch_pull():
            command = {"cmd": "speaker", "op": "pull", "pipe": pipe_id,
                       "bytes": 4}
            result = await self.on_command(node_id, command)
            await self.on_command_completed(node_id, command, result)

        async def pull():
            # Deliberately dispatch the first pull before open_pipe returns.
            await dispatch_pull()
            await dispatch_pull()

        asyncio.create_task(pull())
        await asyncio.sleep(0)
        return pipe_id

    async def send_pipe(self, pipe_id, data, close=False):
        assert pipe_id == 7
        self.sent.extend(data)
        if close:
            self._open_pipes.discard(pipe_id)

    async def send_pipe_streamed(self, pipe_id, data, close=False):
        await self.send_pipe(pipe_id, data, close=close)


def test_speaker_pull_and_completion():
    async def run():
        node = FakeHardwareNode()
        data = bytes(range(8))
        await node.play_buffer(3, data)
        assert node.sent == data
        assert node._speaker is None

    asyncio.run(run())


def test_mic_stream_and_pyro_latch():
    async def run():
        node = FakeHardwareNode()
        stream = await node.start_mic_stream(3)
        await node.on_pipe_data(9, 3, b"abcd")
        assert await stream.read(timeout_ms=10) == b"abcd"
        await node.stop_mic_stream(3)
        assert await stream.read() == b""

        assert await node.set_pyro_enable(3, True) is True
        assert await node.get_pyro_state(3) is True
        assert await node.get_pyro_state(3) is False

    asyncio.run(run())


def test_command_completed_runs_after_callback_result_write():
    class Probe(AsyncPCTransportNode):
        def __init__(self):
            self.steps = []

        async def _write_frame(self, frame_type, request_id, payload=b""):
            assert frame_type == CALLBACK_RESULT
            self.steps.append("result")

        async def on_command(self, src_id, command):
            self.steps.append("command")
            return {"ok": True}

        async def on_command_completed(self, src_id, command, result):
            assert result == {"ok": True}
            self.steps.append("completed")

        def on_callback_error(self, error):
            raise error

    async def run():
        node = Probe()
        payload = bytes((3,)) + json.dumps({"test": True}).encode()
        await node._dispatch_event(ON_COMMAND, 9, payload)
        assert node.steps == ["command", "result", "completed"]

    asyncio.run(run())
