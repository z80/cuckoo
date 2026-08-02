import sys
import os
import asyncio
import time

if os.name == 'nt':
    import ctypes
    # Request 1ms timer resolution from Windows
    ctypes.windll.winmm.timeBeginPeriod(1)

from pc_transport_node_async import AsyncPCTransportNode

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"
TEST_PAYLOAD_SIZE = 32
PING_INTERVAL_SEC = 0.0


class AsyncPCNode(AsyncPCTransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.node_rx_buffers = {}
        self.pipe_data_event = asyncio.Event()

    async def on_command(self, src_id, command):
        # Commenting out the print to keep the console clean during the continuous pipe test
        # print("command from", src_id, command)
        return {"ok": True}

    async def on_pipe_opened(self, pipe_id, src_id):
        print("pipe opened", pipe_id, "from", src_id)

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        # Buffer incoming data by the SOURCE node, not the pipe ID
        if src_id not in self.node_rx_buffers:
            self.node_rx_buffers[src_id] = bytearray()
        
        self.node_rx_buffers[src_id].extend(data_chunk)
        self.pipe_data_event.set()

    async def on_pipe_closed(self, pipe_id, src_id):
        print("pipe closed", pipe_id, "from", src_id)

    def on_callback_error(self, error):
        print("callback error:", error)


async def main():
    node = await AsyncPCNode.create(port=PORT)

    try:
        while await node.get_node_id() is None:
            print("waiting for NRF registration")
            await asyncio.sleep(1.0)

        print("PC node ID:", node.node_id)
        
        qty = await node.get_nodes_qty()
        print("online nodes:", qty)

        target_node = None
        for index in range(qty):
            info = await node.get_node_info(index)
            print("node", index, info)
            
            node_id = info.get("id")
            if node_id is not None and node_id != node.node_id and target_node is None:
                target_node = node_id

        # --- PERIODIC PIPE TEST ROUTINE ---
        if target_node is not None:
            print(f"\n--- Starting Periodic Pipe Echo Test with Node {target_node} ---")
            
            try:
                # Initialize rx buffer for the target node
                node.node_rx_buffers[target_node] = bytearray()
                
                # Open the outgoing pipe once
                out_pipe_id = await node.open_pipe(target_node)
                print(f"Outgoing pipe opened with ID: {out_pipe_id}")
                print(f"Pinging every {PING_INTERVAL_SEC} seconds...\n")
                
                packet_count = 0
                
                # Continuous ping loop
                while True:
                    packet_count += 1
                    test_data = os.urandom(TEST_PAYLOAD_SIZE)
                    
                    # Clear the buffer and event BEFORE sending, to discard any late/stale packets
                    node.node_rx_buffers[target_node].clear()
                    node.pipe_data_event.clear()
                    
                    # Record start time using a high-resolution counter
                    start_time = time.perf_counter()
                    
                    # Send the data
                    await node.send_pipe(out_pipe_id, test_data)
                    
                    timeout_limit = 5.0
                    loop = asyncio.get_event_loop()
                    end_time_limit = loop.time() + timeout_limit
                    
                    try:
                        # Wait for the exact number of bytes to come back
                        while len(node.node_rx_buffers[target_node]) < TEST_PAYLOAD_SIZE:
                            time_left = end_time_limit - loop.time()
                            if time_left <= 0:
                                raise asyncio.TimeoutError()
                            
                            node.pipe_data_event.clear()
                            await asyncio.wait_for(node.pipe_data_event.wait(), timeout=time_left)
                            
                        # Record end time immediately after the payload finishes arriving
                        end_time = time.perf_counter()
                        rtt_ms = (end_time - start_time) * 1000.0
                        
                        received_data = bytes(node.node_rx_buffers[target_node][:TEST_PAYLOAD_SIZE])
                        
                        if received_data == test_data:
                            print(f"[Pkt {packet_count}] SUCCESS: RTT = {rtt_ms:.2f} ms | Data matched ({received_data.hex()})")
                        else:
                            print(f"[Pkt {packet_count}] FAILURE: Data mismatch. Exp {test_data.hex()}, got {received_data.hex()}")
                            
                    except asyncio.TimeoutError:
                        print(f"[Pkt {packet_count}] FAILURE: Timed out waiting for pipe echo reply.")
                    
                    # Wait before sending the next ping
                    await asyncio.sleep(PING_INTERVAL_SEC)
            
            except Exception as e:
                print(f"ERROR during pipe test: {e}")
                
        else:
            print("No remote node available to run pipe test.")
            print("servicing callbacks; press Ctrl-C on the PC to stop")
            while True:
                await asyncio.sleep(3600)

    except asyncio.CancelledError:
        pass
    finally:
        await node.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

