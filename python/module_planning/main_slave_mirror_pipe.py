import asyncio
import gc
from transport_node import TransportNode

class SlaveNode(TransportNode):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Maps incoming pipe_id -> outgoing return pipe_id
        self.echo_pipes = {}

    async def on_command(self, src_id, command):
        print("RX", src_id, command)
        ret = {
            "slave_response": "all good",
        }
        return ret

    # --- UPDATED PIPE HANDLERS ---
    async def on_pipe_opened(self, pipe_id, src_id):
        print("PIPE OPEN", pipe_id, "from", src_id)
        try:
            # Open a new return pipe back to the sender
            ret_pipe = await self.open_pipe(src_id)
            self.echo_pipes[pipe_id] = ret_pipe
            print("RETURN PIPE OPENED", ret_pipe, "to", src_id)
        except Exception as e:
            print("ERROR OPENING RETURN PIPE:", e)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        #print("PIPE DATA", pipe_id, "from", src_id, "len:", len(data_chunk))
        try:
            # Look up the return pipe for this incoming connection
            ret_pipe = self.echo_pipes.get(pipe_id)
            if ret_pipe is not None:
                # Echo data on the return pipe
                await self.send_pipe(ret_pipe, data_chunk)
            else:
                print("NO RETURN PIPE FOUND FOR", pipe_id)
        except Exception as e:
            print("PIPE ECHO ERROR:", e)

    async def on_pipe_closed(self, pipe_id, src_id):
        print("PIPE CLOSED", pipe_id, "from", src_id)
        # Clean up and close the return pipe as well
        ret_pipe = self.echo_pipes.pop(pipe_id, None)
        if ret_pipe is not None:
            try:
                await self.send_pipe(ret_pipe, b"", close=True)
                print("RETURN PIPE CLOSED", ret_pipe)
            except Exception as e:
                print("ERROR CLOSING RETURN PIPE:", e)
    # -----------------------------

    async def periodic_task(self):
        while self.node_id is None:
            print("WAIT enum")
            await asyncio.sleep(2)

        print("ID", self.node_id)
        print("LOOP start")

        while True:
            try:
                qty = await self.get_nodes_qty()
                print("NODES", qty)

                for idx in range(qty):
                    info = await self.get_node_info(idx)
                    
                    node_id = info.get("id")
                    if node_id is None or node_id == self.node_id:
                        continue
                    
                    free_bytes = gc.mem_free()
                    cmd = {
                        "cmd": "ping",
                        "from": self.node_id,
                        "to": node_id,
                        "message": "Hello from slave {}".format(self.node_id), 
                        "slave free bytes": free_bytes
                    }

                    try:
                        reply = await self.send_command_and_wait_reply(node_id, cmd)
                    except Exception as e:
                        pass # Silently fail ping to keep console clean during pipe test

            except Exception as e:
                print("LOOP!", e)

            await asyncio.sleep(5)


async def async_main():
    tr = SlaveNode()
    #asyncio.create_task(tr.periodic_task())
    await tr.process()


def main():
    asyncio.run(async_main())

main()

