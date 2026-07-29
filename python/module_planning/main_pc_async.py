import sys
import asyncio

# Assuming the async class is saved in this file
from pc_transport_node_async import AsyncPCTransportNode


PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"


class AsyncPCNode(AsyncPCTransportNode):
    async def on_command(self, src_id, command):
        print("command from", src_id, command)
        return {"ok": True}

    async def on_pipe_opened(self, pipe_id, src_id):
        print("pipe opened", pipe_id, "from", src_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        print("pipe data", pipe_id, "from", src_id, repr(data_chunk))

    async def on_pipe_closed(self, pipe_id, src_id):
        print("pipe closed", pipe_id, "from", src_id)

    def on_callback_error(self, error):
        print("callback error:", error)


async def main():
    # Asynchronous initialization via the class factory method
    node = await AsyncPCNode.create(port=PORT)

    try:
        # Await network registration
        while await node.get_node_id() is None:
            print("waiting for NRF registration")
            # We don't need node.poll() here anymore; the background task reads automatically
            await asyncio.sleep(1.0)

        print("PC node ID:", node.node_id)
        
        qty = await node.get_nodes_qty()
        print("online nodes:", qty)

        for index in range(qty):
            info = await node.get_node_info(index)
            print("node", index, info)

        print("servicing callbacks; press Ctrl-C on the PC to stop")
        
        # Keep the main coroutine alive forever so the background listener can work
        while True:
            await asyncio.sleep(3600)

    except asyncio.CancelledError:
        # Handle internal async cancellation gracefully
        pass
    finally:
        await node.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Catch Ctrl-C to exit cleanly
        pass

