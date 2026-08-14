import asyncio

from hardware_node.pc_hardware_node import PCHardwareNode


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

        async def pull():
            await asyncio.sleep(0)
            await self.on_command(
                node_id,
                {"cmd": "speaker", "op": "pull", "pipe": pipe_id,
                 "bytes": 4},
            )
            await self.on_command(
                node_id,
                {"cmd": "speaker", "op": "pull", "pipe": pipe_id,
                 "bytes": 4},
            )

        asyncio.create_task(pull())
        return pipe_id

    async def send_pipe(self, pipe_id, data, close=False):
        assert pipe_id == 7
        self.sent.extend(data)
        if close:
            self._open_pipes.discard(pipe_id)


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
