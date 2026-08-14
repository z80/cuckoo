import uasyncio as asyncio

from mic import Mic
from pyro import Pyro
from speaker import Speaker
from transport_node import TransportNode


class HardwareNode(TransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mic = Mic()
        self.speaker = Speaker()
        self.pyro = Pyro()

    async def on_command(self, src_id, command):
        if not isinstance(command, dict):
            return {"err": "command"}
        device = command.get("cmd")
        operation = command.get("op")
        if device == "mic":
            return await self.mic.handle_command(self, src_id, operation)
        if device == "speaker":
            return await self.speaker.handle_command(
                self, src_id, operation, command)
        if device == "pyro":
            if operation == "enable":
                return self.pyro.set_enable(command.get("enable", False))
            if operation == "state":
                return self.pyro.get_state()
            return {"err": "op"}
        return {"err": "command"}

    async def on_pipe_opened(self, pipe_id, src_id):
        await self.speaker.on_pipe_opened(pipe_id, src_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        await self.speaker.on_pipe_data(pipe_id, src_id, data_chunk)

    async def on_pipe_closed(self, pipe_id, src_id):
        await self.speaker.on_pipe_closed(pipe_id, src_id)

    async def on_pipe_failed(self, pipe_id, src_id, reason, transferred):
        await self.speaker.on_pipe_failed(
            pipe_id, src_id, reason, transferred)

    def shutdown_hardware(self):
        self.mic.shutdown()
        self.speaker.shutdown()
        self.pyro.shutdown()


async def async_main():
    node = HardwareNode(irq_pin="A3")
    pyro_task = asyncio.create_task(node.pyro.process())
    try:
        await node.process()
    finally:
        node.shutdown_hardware()
        pyro_task.cancel()


def main():
    asyncio.run(async_main())


main()
